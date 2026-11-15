
import hashlib
import json
import stat
import zipfile

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

from django.conf import settings


CHUNK_SIZE = 1024 * 1024


class PackageValidationError(Exception):
    """Ошибка проверки установочного пакета."""


@dataclass(frozen=True)
class PackageValidationResult:
    version: str
    sha256: str
    size_bytes: int
    manifest: dict[str, Any]
    file_count: int
    uncompressed_size_bytes: int


def _get_archive_limits() -> dict[str, int]:
    defaults = {
        "max_entries": 5000,
        "max_total_size": 1024 * 1024 * 1024,
        "max_file_size": 256 * 1024 * 1024,
        "max_compression_ratio": 200,
        "max_manifest_size": 65536,
    }

    configured = getattr(
        settings,
        "DEPLOYMENT_ARCHIVE_LIMITS",
        {},
    )

    if not isinstance(configured, dict):
        raise PackageValidationError(
            "Некорректная конфигурация ограничений ZIP-архивов."
        )

    limits = defaults.copy()

    for key in defaults:
        if key in configured:
            value = configured[key]

            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise PackageValidationError(
                    f"Некорректное ограничение архива: {key}."
                )

            limits[key] = value

    return limits


def _validate_member_path(value: str) -> str:
    """
    Проверяет имя файла внутри ZIP.

    Запрещает абсолютные пути, traversal,
    Windows-диски и опасные имена.
    """

    if not value or "\x00" in value:
        raise PackageValidationError(
            "В архиве обнаружено пустое или некорректное имя файла."
        )

    if "\\" in value or value.startswith("/"):
        raise PackageValidationError(
            f"Недопустимый путь в архиве: {value!r}."
        )

    windows_path = PureWindowsPath(value)

    if windows_path.drive or windows_path.root:
        raise PackageValidationError(
            f"Абсолютные пути запрещены: {value!r}."
        )

    parts = value.split("/")

    if any(part in ("", ".", "..") for part in parts):
        raise PackageValidationError(
            f"Недопустимый путь в архиве: {value!r}."
        )

    forbidden_chars = set('<>:"|?*')

    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *{f"COM{i}" for i in range(1, 10)},
        *{f"LPT{i}" for i in range(1, 10)},
    }

    for part in parts:
        if part.endswith((" ", ".")):
            raise PackageValidationError(
                f"Недопустимое имя файла: {part!r}."
            )

        if any(ord(char) < 32 for char in part):
            raise PackageValidationError(
                "Управляющие символы в именах файлов запрещены."
            )

        if any(char in forbidden_chars for char in part):
            raise PackageValidationError(
                f"Недопустимые символы в имени файла: {part!r}."
            )

        if part.split(".")[0].upper() in reserved_names:
            raise PackageValidationError(
                f"Недопустимое системное имя: {part!r}."
            )

    return "/".join(parts)


def _validate_archive(
    archive: zipfile.ZipFile,
    limits: dict[str, int],
) -> tuple[dict[str, Any], int, int]:
    infos = archive.infolist()

    if not infos:
        raise PackageValidationError(
            "ZIP-архив пуст."
        )

    if len(infos) > limits["max_entries"]:
        raise PackageValidationError(
            "В архиве слишком много файлов и каталогов."
        )

    seen_names = set()
    file_names = set()

    total_size = 0
    file_count = 0
    manifest_info = None

    reserved_names = {
        ".deployment-release.json",
        "__deployment_backup__.json",
    }

    for info in infos:
        is_directory = info.is_dir()

        raw_name = (
            info.filename[:-1]
            if is_directory
            else info.filename
        )

        safe_name = _validate_member_path(raw_name)
        name_key = safe_name.casefold()

        if name_key in seen_names:
            raise PackageValidationError(
                f"В архиве повторяется имя: {raw_name!r}."
            )

        seen_names.add(name_key)

        if name_key in {
            name.casefold() for name in reserved_names
        }:
            raise PackageValidationError(
                f"Недопустимый служебный файл: {raw_name!r}."
            )

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)

        if file_type == stat.S_IFLNK:
            raise PackageValidationError(
                f"Символические ссылки запрещены: {raw_name!r}."
            )

        if file_type not in (
            0,
            stat.S_IFREG,
            stat.S_IFDIR,
        ):
            raise PackageValidationError(
                f"Специальные файлы запрещены: {raw_name!r}."
            )

        if file_type == stat.S_IFDIR and not is_directory:
            raise PackageValidationError(
                f"Некорректный тип каталога: {raw_name!r}."
            )

        if file_type == stat.S_IFREG and is_directory:
            raise PackageValidationError(
                f"Некорректный тип файла: {raw_name!r}."
            )

        if info.flag_bits & 0x1:
            raise PackageValidationError(
                "Зашифрованные ZIP-архивы не поддерживаются."
            )

        if is_directory:
            continue

        file_count += 1

        if info.file_size > limits["max_file_size"]:
            raise PackageValidationError(
                f"Файл превышает допустимый размер: {raw_name!r}."
            )

        total_size += info.file_size

        if total_size > limits["max_total_size"]:
            raise PackageValidationError(
                "Общий распакованный размер архива превышает лимит."
            )

        if info.file_size > 0:
            if info.compress_size <= 0:
                raise PackageValidationError(
                    f"Некорректный размер сжатого файла: {raw_name!r}."
                )

            ratio = info.file_size / info.compress_size

            if ratio > limits["max_compression_ratio"]:
                raise PackageValidationError(
                    f"Слишком высокий коэффициент сжатия: {raw_name!r}."
                )

        file_names.add(name_key)

        if safe_name == "manifest.json":
            manifest_info = info

    # Проверяем конфликты вида:
    # файл "application" и одновременно "application/main.py".
    for file_name in file_names:
        parts = file_name.split("/")

        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])

            if parent in file_names:
                raise PackageValidationError(
                    "В архиве обнаружен конфликт файлов и каталогов."
                )

    if manifest_info is None:
        raise PackageValidationError(
            "В корне архива отсутствует manifest.json."
        )

    if manifest_info.file_size == 0:
        raise PackageValidationError(
            "manifest.json пуст."
        )

    if manifest_info.file_size > limits["max_manifest_size"]:
        raise PackageValidationError(
            "manifest.json превышает допустимый размер."
        )

    if file_count < 2:
        raise PackageValidationError(
            "Архив должен содержать manifest.json и файлы приложения."
        )

    # CRC-проверка всех файлов.
    try:
        damaged_file = archive.testzip()
    except Exception as exc:
        raise PackageValidationError(
            "Не удалось проверить целостность ZIP-архива."
        ) from exc

    if damaged_file is not None:
        raise PackageValidationError(
            f"Ошибка CRC в файле {damaged_file!r}."
        )

    try:
        manifest_raw = archive.read(manifest_info)
        manifest = json.loads(
            manifest_raw.decode("utf-8")
        )
    except Exception as exc:
        raise PackageValidationError(
            "Не удалось прочитать manifest.json."
        ) from exc

    if not isinstance(manifest, dict):
        raise PackageValidationError(
            "manifest.json должен содержать JSON-объект."
        )

    version = manifest.get("version")

    if (
        not isinstance(version, str)
        or not version.strip()
        or version != version.strip()
        or len(version) > 100
    ):
        raise PackageValidationError(
            "В manifest.json отсутствует корректная версия."
        )

    return manifest, file_count, total_size


def inspect_uploaded_package(uploaded_file) -> PackageValidationResult:
    """
    Проверяет загруженный ZIP и вычисляет SHA-256.

    Файл не распаковывается и не запускается.
    """

    filename = getattr(uploaded_file, "name", "")

    if not filename or not filename.lower().endswith(".zip"):
        raise PackageValidationError(
            "Разрешена загрузка только ZIP-файлов."
        )

    max_package_size = getattr(
        settings,
        "SOFTWARE_PACKAGE_MAX_SIZE_BYTES",
        256 * 1024 * 1024,
    )

    uploaded_size = getattr(
        uploaded_file,
        "size",
        None,
    )

    if (
        uploaded_size is not None
        and uploaded_size > max_package_size
    ):
        raise PackageValidationError(
            "Размер ZIP-пакета превышает допустимый лимит."
        )

    uploaded_file.seek(0)

    digest = hashlib.sha256()
    actual_size = 0

    try:
        while True:
            chunk = uploaded_file.read(CHUNK_SIZE)

            if not chunk:
                break

            actual_size += len(chunk)

            if actual_size > max_package_size:
                raise PackageValidationError(
                    "Размер ZIP-пакета превышает допустимый лимит."
                )

            digest.update(chunk)

        if actual_size == 0:
            raise PackageValidationError(
                "Загруженный ZIP-файл пуст."
            )

        uploaded_file.seek(0)

        limits = _get_archive_limits()

        with zipfile.ZipFile(uploaded_file, "r") as archive:
            manifest, file_count, uncompressed_size = (
                _validate_archive(
                    archive,
                    limits,
                )
            )

        return PackageValidationResult(
            version=manifest["version"],
            sha256=digest.hexdigest(),
            size_bytes=actual_size,
            manifest=manifest,
            file_count=file_count - 1,
            uncompressed_size_bytes=uncompressed_size,
        )

    except PackageValidationError:
        raise
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise PackageValidationError(
            "Файл не является корректным ZIP-архивом."
        ) from exc
    finally:
        uploaded_file.seek(0)
