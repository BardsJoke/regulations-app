
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile

from datetime import datetime, timezone as datetime_timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from django.conf import settings
from django.core.files import File as DjangoFile
from django.core.files.storage import default_storage
from django.utils import timezone

from backups.models import Backup
from backups.services import create_backup as create_backup_record
from backups.services import verify_backup

from deployments.models import DeploymentAdapterType

from .base import (
    BaseDeploymentAdapter,
    DeploymentActionResult,
    DeploymentAdapterError,
    DeploymentConfigurationError,
    DeploymentInspection,
    HealthCheckResult,
    PackagePreflight,
)


class LocalFilesystemAdapter(BaseDeploymentAdapter):
    """
    Адаптер развёртывания программного обеспечения
    в локальной файловой песочнице.

    Структура каталога целевого окружения:

        target/
            current.json
            releases/
                rel-<uuid>/
                    manifest.json
                    .deployment-release.json
                    application/
                        ...

    current.json указывает на активную версию.

    Резервные копии хранятся отдельно через существующую
    модель Backup и backups.services.create_backup().
    """

    POINTER_SCHEMA_VERSION = 1
    RELEASE_SCHEMA_VERSION = 1
    BACKUP_SCHEMA_VERSION = 1

    RELEASE_ID_PREFIX = "rel-"

    CURRENT_POINTER_NAME = "current.json"
    RELEASES_DIR_NAME = "releases"
    RELEASE_METADATA_NAME = ".deployment-release.json"
    BACKUP_METADATA_NAME = "__deployment_backup__.json"
    MANIFEST_NAME = "manifest.json"

    CHUNK_SIZE = 1024 * 1024

    def __init__(self, target):
        super().__init__(target)

        if target.adapter_type != DeploymentAdapterType.LOCAL_FILESYSTEM:
            raise DeploymentConfigurationError(
                "Для этого адаптера целевой объект должен иметь "
                "тип LOCAL_FILESYSTEM."
            )

        if not target.is_configured:
            raise DeploymentConfigurationError(
                "Целевое окружение не настроено."
            )

        self.config = self._load_target_config()
        self.root = self._resolve_target_root()

        self.releases_root = self.root / self.RELEASES_DIR_NAME
        self.current_pointer = self.root / self.CURRENT_POINTER_NAME

        self.healthcheck_file = self._load_healthcheck_file()
        self.archive_limits = self._load_archive_limits()

        self._check_backup_storage_location()

    # ==========================================================
    # Конфигурация и проверка путей
    # ==========================================================

    def _load_target_config(self) -> dict[str, Any]:
        targets = getattr(settings, "DEPLOYMENT_TARGETS", {})

        if not isinstance(targets, dict):
            raise DeploymentConfigurationError(
                "DEPLOYMENT_TARGETS должен быть словарём."
            )

        config = targets.get(self.target.config_key)

        if not isinstance(config, dict):
            raise DeploymentConfigurationError(
                f"В настройках Django отсутствует конфигурация "
                f"целевого окружения '{self.target.config_key}'."
            )

        if "root" not in config:
            raise DeploymentConfigurationError(
                "В конфигурации целевого окружения не указан root."
            )

        return config

    def _resolve_target_root(self) -> Path:
        sandbox_value = getattr(
            settings,
            "DEPLOYMENT_SANDBOX_ROOT",
            None,
        )

        if sandbox_value is None:
            raise DeploymentConfigurationError(
                "В настройках не задан DEPLOYMENT_SANDBOX_ROOT."
            )

        try:
            sandbox_root = Path(sandbox_value).resolve()
            target_root = Path(self.config["root"]).resolve()
        except (TypeError, ValueError, OSError) as exc:
            raise DeploymentConfigurationError(
                "Некорректный путь к песочнице или целевому окружению."
            ) from exc

        # Запрещаем использовать сам корень песочницы
        # в качестве корня отдельного целевого окружения.
        if target_root == sandbox_root:
            raise DeploymentConfigurationError(
                "Корень целевого окружения не может совпадать "
                "с корнем всей песочницы."
            )

        # Целевой каталог обязан находиться внутри песочницы.
        if sandbox_root not in target_root.parents:
            raise DeploymentConfigurationError(
                "Целевой каталог находится за пределами "
                "DEPLOYMENT_SANDBOX_ROOT."
            )

        return target_root

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        first = first.resolve()
        second = second.resolve()

        return (
            first == second
            or first in second.parents
            or second in first.parents
        )

    def _check_backup_storage_location(self) -> None:
        """
        Проверяет, что резервные копии не будут храниться
        внутри каталога устанавливаемого приложения.
        """

        try:
            backup_storage_root = Path(
                default_storage.path("backups")
            ).resolve()
        except (NotImplementedError, AttributeError):
            # Для удалённого хранилища локальный путь неизвестен.
            return

        if self._paths_overlap(self.root, backup_storage_root):
            raise DeploymentConfigurationError(
                "Хранилище резервных копий пересекается "
                "с каталогом целевого окружения. "
                "Разместите MEDIA_ROOT отдельно от песочницы."
            )

    def _load_healthcheck_file(self) -> str | None:
        value = self.config.get("healthcheck_file")

        if not value:
            return None

        try:
            return self._validate_relative_path(str(value))
        except ValueError as exc:
            raise DeploymentConfigurationError(
                "Некорректный healthcheck_file в конфигурации."
            ) from exc

    def _load_archive_limits(self) -> dict[str, int]:
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
            raise DeploymentConfigurationError(
                "DEPLOYMENT_ARCHIVE_LIMITS должен быть словарём."
            )

        result = defaults.copy()

        for key in defaults:
            if key in configured:
                value = configured[key]

                if isinstance(value, bool) or not isinstance(value, int):
                    raise DeploymentConfigurationError(
                        f"Некорректное значение ограничения архива: {key}."
                    )

                if value <= 0:
                    raise DeploymentConfigurationError(
                        f"Ограничение архива {key} должно быть больше нуля."
                    )

                result[key] = value

        return result

    # ==========================================================
    # Безопасная работа с именами файлов из ZIP
    # ==========================================================

    @staticmethod
    def _validate_relative_path(value: str) -> str:
        """
        Проверяет относительный путь.

        Не допускает абсолютные пути, .., Windows-диски,
        обратные слеши и недопустимые для Windows имена.
        """

        if not value or "\x00" in value:
            raise ValueError("Пустой путь или NUL-символ.")

        if "\\" in value:
            raise ValueError("Обратные слеши запрещены.")

        if value.startswith("/"):
            raise ValueError("Абсолютный путь запрещён.")

        windows_path = PureWindowsPath(value)

        if windows_path.drive or windows_path.root:
            raise ValueError("Windows-диски и корневые пути запрещены.")

        parts = value.split("/")

        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Недопустимый компонент пути.")

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
                raise ValueError(
                    "Имя файла не может заканчиваться пробелом или точкой."
                )

            if any(ord(char) < 32 for char in part):
                raise ValueError("Управляющие символы запрещены.")

            if any(char in forbidden_chars for char in part):
                raise ValueError("Имя содержит недопустимые символы.")

            device_name = part.split(".")[0].upper()

            if device_name in reserved_names:
                raise ValueError("Недопустимое системное имя файла.")

        return "/".join(parts)

    def _validate_archive(
        self,
        archive: zipfile.ZipFile,
        *,
        expected_version: str | None = None,
        require_backup_metadata: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """
        Проверяет структуру ZIP до распаковки.

        Проверяются:
        - количество записей;
        - размер распакованных данных;
        - размер отдельных файлов;
        - коэффициент сжатия;
        - CRC;
        - дубли имён;
        - traversal и абсолютные пути;
        - символические ссылки и специальные файлы;
        - manifest.json;
        - metadata резервной копии, если она требуется.
        """

        infos = archive.infolist()

        if not infos:
            raise DeploymentAdapterError(
                "ZIP-архив пуст."
            )

        if len(infos) > self.archive_limits["max_entries"]:
            raise DeploymentAdapterError(
                "В архиве слишком много файлов и каталогов."
            )

        seen_names: set[str] = set()
        file_names: set[str] = set()

        total_size = 0
        regular_file_count = 0

        manifest_info = None
        backup_metadata_info = None

        for info in infos:
            raw_name = info.filename

            is_directory = info.is_dir()
            normalized_raw = raw_name[:-1] if is_directory else raw_name

            try:
                safe_name = self._validate_relative_path(normalized_raw)
            except ValueError as exc:
                raise DeploymentAdapterError(
                    f"В архиве обнаружен небезопасный путь: {raw_name!r}."
                ) from exc

            name_key = safe_name.casefold()

            if name_key in seen_names:
                raise DeploymentAdapterError(
                    f"В архиве есть повторяющиеся имена: {raw_name!r}."
                )

            seen_names.add(name_key)

            # Проверка Unix-атрибутов ZIP.
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)

            if file_type == stat.S_IFLNK:
                raise DeploymentAdapterError(
                    f"Символические ссылки запрещены: {raw_name!r}."
                )

            if file_type not in (
                0,
                stat.S_IFREG,
                stat.S_IFDIR,
            ):
                raise DeploymentAdapterError(
                    f"Специальные типы файлов запрещены: {raw_name!r}."
                )

            if file_type == stat.S_IFDIR and not is_directory:
                raise DeploymentAdapterError(
                    f"Некорректный тип каталога: {raw_name!r}."
                )

            if file_type == stat.S_IFREG and is_directory:
                raise DeploymentAdapterError(
                    f"Некорректный тип файла: {raw_name!r}."
                )

            if info.flag_bits & 0x1:
                raise DeploymentAdapterError(
                    "Зашифрованные ZIP-файлы не поддерживаются."
                )

            if is_directory:
                continue

            regular_file_count += 1

            if info.file_size > self.archive_limits["max_file_size"]:
                raise DeploymentAdapterError(
                    f"Файл слишком большой: {raw_name!r}."
                )

            total_size += info.file_size

            if total_size > self.archive_limits["max_total_size"]:
                raise DeploymentAdapterError(
                    "Общий распакованный размер архива превышает лимит."
                )

            if info.file_size > 0:
                if info.compress_size <= 0:
                    raise DeploymentAdapterError(
                        f"Некорректное сжатие файла: {raw_name!r}."
                    )

                ratio = info.file_size / info.compress_size

                if ratio > self.archive_limits["max_compression_ratio"]:
                    raise DeploymentAdapterError(
                        f"Слишком высокий коэффициент сжатия: {raw_name!r}."
                    )

            file_names.add(name_key)

            if safe_name == self.MANIFEST_NAME:
                manifest_info = info

            if safe_name == self.BACKUP_METADATA_NAME:
                backup_metadata_info = info

            if safe_name == self.RELEASE_METADATA_NAME:
                raise DeploymentAdapterError(
                    "Архив не может содержать служебный файл "
                    ".deployment-release.json."
                )

        if regular_file_count == 0:
            raise DeploymentAdapterError(
                "В архиве нет файлов."
            )

        # Проверяем конфликт вида:
        # файл "application" и одновременно "application/main.py".
        for file_name in file_names:
            parts = file_name.split("/")

            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])

                if parent in file_names:
                    raise DeploymentAdapterError(
                        "В архиве обнаружен конфликт файлов и каталогов."
                    )

        if manifest_info is None:
            raise DeploymentAdapterError(
                "В корне ZIP-архива отсутствует manifest.json."
            )

        if manifest_info.file_size > self.archive_limits["max_manifest_size"]:
            raise DeploymentAdapterError(
                "Файл manifest.json превышает допустимый размер."
            )

        if manifest_info.file_size == 0:
            raise DeploymentAdapterError(
                "Файл manifest.json пуст."
            )

        if require_backup_metadata and backup_metadata_info is None:
            raise DeploymentAdapterError(
                "В архиве отсутствует метадата резервной копии."
            )

        try:
            broken_file = archive.testzip()
        except (
            zipfile.BadZipFile,
            RuntimeError,
            OSError,
            EOFError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось проверить целостность ZIP-архива."
            ) from exc

        if broken_file is not None:
            raise DeploymentAdapterError(
                f"Ошибка CRC при проверке файла {broken_file!r}."
            )

        try:
            manifest_raw = archive.read(manifest_info)
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось прочитать manifest.json."
            ) from exc

        if not isinstance(manifest, dict):
            raise DeploymentAdapterError(
                "manifest.json должен содержать JSON-объект."
            )

        version = manifest.get("version")

        if not isinstance(version, str) or not version.strip():
            raise DeploymentAdapterError(
                "В manifest.json отсутствует корректное поле version."
            )

        if expected_version is not None and version != str(expected_version):
            raise DeploymentAdapterError(
                f"Версия в архиве ({version}) не совпадает "
                f"с целевой версией ({expected_version})."
            )

        backup_metadata = None

        if backup_metadata_info is not None:
            try:
                metadata_raw = archive.read(backup_metadata_info)

                if len(metadata_raw) > self.archive_limits["max_manifest_size"]:
                    raise DeploymentAdapterError(
                        "Метадата резервной копии слишком большая."
                    )

                backup_metadata = json.loads(
                    metadata_raw.decode("utf-8")
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                OSError,
                RuntimeError,
            ) as exc:
                raise DeploymentAdapterError(
                    "Не удалось прочитать метадату резервной копии."
                ) from exc

            if not isinstance(backup_metadata, dict):
                raise DeploymentAdapterError(
                    "Некорректный формат метадаты резервной копии."
                )

        payload_count = sum(
            1
            for info in infos
            if not info.is_dir()
            and info.filename not in (
                self.MANIFEST_NAME,
                self.BACKUP_METADATA_NAME,
            )
        )

        if payload_count == 0:
            raise DeploymentAdapterError(
                "В архиве нет файлов приложения."
            )

        return manifest, backup_metadata

    # ==========================================================
    # Распаковка и служебные файлы
    # ==========================================================

    def _extract_archive(
        self,
        archive: zipfile.ZipFile,
        destination: Path,
        *,
        skip_root_names: set[str] | None = None,
    ) -> None:
        """
        Распаковывает архив вручную.

        zipfile.extract() и extractall() намеренно не используются.
        """

        skip_root_names = {
            name.casefold()
            for name in (skip_root_names or set())
        }

        destination.mkdir(parents=True, exist_ok=True)
        destination_resolved = destination.resolve()

        for info in archive.infolist():
            is_directory = info.is_dir()
            raw_name = info.filename
            normalized_name = raw_name[:-1] if is_directory else raw_name

            safe_name = self._validate_relative_path(normalized_name)

            if "/" not in safe_name and safe_name.casefold() in skip_root_names:
                continue

            parts = PurePosixPath(safe_name).parts
            target_path = destination.joinpath(*parts)

            if not target_path.resolve().is_relative_to(destination_resolved):
                raise DeploymentAdapterError(
                    "Путь распаковки выходит за пределы временного каталога."
                )

            if is_directory:
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)

            written = 0

            try:
                with archive.open(info, "r") as source:
                    with target_path.open("xb") as destination_file:
                        while True:
                            chunk = source.read(self.CHUNK_SIZE)

                            if not chunk:
                                break

                            written += len(chunk)

                            if written > self.archive_limits["max_file_size"]:
                                raise DeploymentAdapterError(
                                    "Размер распакованного файла превысил лимит."
                                )

                            destination_file.write(chunk)

            except FileExistsError as exc:
                raise DeploymentAdapterError(
                    f"Файл уже существует при распаковке: {safe_name}."
                ) from exc
            except (
                zipfile.BadZipFile,
                RuntimeError,
                OSError,
            ) as exc:
                raise DeploymentAdapterError(
                    f"Ошибка распаковки файла {safe_name}."
                ) from exc

            if written != info.file_size:
                raise DeploymentAdapterError(
                    f"Размер распакованного файла не совпадает "
                    f"с размером в архиве: {safe_name}."
                )

    def _write_json_file(
        self,
        path: Path,
        data: dict[str, Any],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("x", encoding="utf-8") as destination:
            json.dump(
                data,
                destination,
                ensure_ascii=False,
                indent=2,
            )
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())

    def _write_current_pointer(
        self,
        *,
        release_id: str,
        version: str,
    ) -> None:
        """
        Атомарно переключает активную версию.

        Сначала создаётся временный JSON, затем os.replace()
        заменяет current.json.
        """

        self.root.mkdir(parents=True, exist_ok=True)

        if self.current_pointer.is_symlink():
            raise DeploymentAdapterError(
                "current.json не должен быть символической ссылкой."
            )

        pointer_data = {
            "schema_version": self.POINTER_SCHEMA_VERSION,
            "release_id": release_id,
            "version": version,
            "updated_at": timezone.now().isoformat(),
        }

        temporary_path = self.root / f".current-{uuid.uuid4().hex}.tmp"

        try:
            with temporary_path.open("x", encoding="utf-8") as destination:
                json.dump(
                    pointer_data,
                    destination,
                    ensure_ascii=False,
                    indent=2,
                )
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())

            os.replace(temporary_path, self.current_pointer)

        except OSError as exc:
            raise DeploymentAdapterError(
                "Не удалось переключить активную версию приложения."
            ) from exc

        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_release_metadata(
        self,
        release_path: Path,
        *,
        release_id: str,
        version: str,
    ) -> None:
        metadata = {
            "schema_version": self.RELEASE_SCHEMA_VERSION,
            "target_id": str(self.target.pk),
            "release_id": release_id,
            "version": version,
            "created_at": timezone.now().isoformat(),
        }

        self._write_json_file(
            release_path / self.RELEASE_METADATA_NAME,
            metadata,
        )

    # ==========================================================
    # Чтение активной версии
    # ==========================================================

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise DeploymentAdapterError(
                f"Ожидаемый служебный файл отсутствует: {path.name}."
            )

        try:
            with path.open("r", encoding="utf-8") as source:
                data = json.load(source)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise DeploymentAdapterError(
                f"Не удалось прочитать служебный файл {path.name}."
            ) from exc

        if not isinstance(data, dict):
            raise DeploymentAdapterError(
                f"Некорректный формат файла {path.name}."
            )

        return data

    def _get_current_release(self) -> tuple[dict[str, Any], Path] | None:
        if not self.current_pointer.exists():
            return None

        if self.current_pointer.is_symlink():
            raise DeploymentAdapterError(
                "current.json не должен быть символической ссылкой."
            )

        pointer = self._read_json_file(self.current_pointer)

        if pointer.get("schema_version") != self.POINTER_SCHEMA_VERSION:
            raise DeploymentAdapterError(
                "Неподдерживаемая версия формата current.json."
            )

        release_id = pointer.get("release_id")

        if (
            not isinstance(release_id, str)
            or not release_id.startswith(self.RELEASE_ID_PREFIX)
            or len(release_id) != len(self.RELEASE_ID_PREFIX) + 32
        ):
            raise DeploymentAdapterError(
                "В current.json указан некорректный release_id."
            )

        release_suffix = release_id[len(self.RELEASE_ID_PREFIX):]

        if any(char not in "0123456789abcdef" for char in release_suffix):
            raise DeploymentAdapterError(
                "В current.json указан некорректный release_id."
            )

        release_path = self.releases_root / release_id

        if release_path.is_symlink() or not release_path.is_dir():
            raise DeploymentAdapterError(
                "Каталог активной версии отсутствует или недоступен."
            )

        if release_path.resolve().parent != self.releases_root.resolve():
            raise DeploymentAdapterError(
                "Каталог активной версии находится вне releases."
            )

        release_metadata = self._read_json_file(
            release_path / self.RELEASE_METADATA_NAME
        )

        if release_metadata.get("target_id") != str(self.target.pk):
            raise DeploymentAdapterError(
                "Активная версия относится к другому целевому окружению."
            )

        if release_metadata.get("release_id") != release_id:
            raise DeploymentAdapterError(
                "Идентификатор активной версии не совпадает с метаданными."
            )

        manifest = self._read_json_file(
            release_path / self.MANIFEST_NAME
        )

        version = manifest.get("version")

        if not isinstance(version, str) or not version.strip():
            raise DeploymentAdapterError(
                "В manifest.json активной версии отсутствует version."
            )

        if (
            release_metadata.get("version") != version
            or pointer.get("version") != version
        ):
            raise DeploymentAdapterError(
                "Версия приложения не совпадает в служебных файлах."
            )

        return pointer, release_path

    # ==========================================================
    # Контракт BaseDeploymentAdapter: inspect
    # ==========================================================

    def inspect(self) -> DeploymentInspection:
        current = self._get_current_release()

        if current is None:
            return DeploymentInspection(
                target_id=str(self.target.pk),
                installed=False,
                current_version=None,
                release_id=None,
                release_path=None,
                message="В целевом окружении пока нет установленной версии.",
                details={
                    "root": str(self.root),
                    "adapter": "LOCAL_FILESYSTEM",
                },
            )

        pointer, release_path = current

        return DeploymentInspection(
            target_id=str(self.target.pk),
            installed=True,
            current_version=pointer["version"],
            release_id=pointer["release_id"],
            release_path=str(release_path),
            message="Активная версия найдена и прошла проверку метаданных.",
            details={
                "root": str(self.root),
                "release_id": pointer["release_id"],
                "adapter": "LOCAL_FILESYSTEM",
            },
        )

    # ==========================================================
    # Контракт BaseDeploymentAdapter: preflight_package
    # ==========================================================

    def preflight_package(self, ticket) -> PackagePreflight:
        if ticket.deployment_target_id != self.target.pk:
            raise DeploymentAdapterError(
                "Пакет заявки относится к другому целевому окружению."
            )

        package = ticket.package_file

        if not package or not package.name:
            raise DeploymentAdapterError(
                "В заявке отсутствует файл установочного пакета."
            )

        digest = hashlib.sha256()
        package_size = 0

        try:
            with package.open("rb") as source:
                while True:
                    chunk = source.read(self.CHUNK_SIZE)

                    if not chunk:
                        break

                    digest.update(chunk)
                    package_size += len(chunk)

                source.seek(0)

                with zipfile.ZipFile(source, "r") as archive:
                    self._validate_archive(
                        archive,
                        expected_version=str(ticket.target_version),
                    )

                    file_count = sum(
                        1
                        for info in archive.infolist()
                        if not info.is_dir()
                        and info.filename != self.MANIFEST_NAME
                    )

                    uncompressed_size = sum(
                        info.file_size
                        for info in archive.infolist()
                        if not info.is_dir()
                    )

        except DeploymentAdapterError:
            raise
        except (
            zipfile.BadZipFile,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось прочитать установочный ZIP-пакет."
            ) from exc

        actual_sha256 = digest.hexdigest()
        expected_sha256 = (ticket.expected_sha256 or "").strip().lower()

        if expected_sha256 and actual_sha256 != expected_sha256:
            raise DeploymentAdapterError(
                "SHA-256 установочного пакета не совпадает "
                "с ожидаемым значением."
            )

        if package_size <= 0:
            raise DeploymentAdapterError(
                "Установочный пакет пуст."
            )

        return PackagePreflight(
            version=str(ticket.target_version),
            sha256=actual_sha256,
            size_bytes=package_size,
            file_count=file_count,
            uncompressed_size_bytes=uncompressed_size,
        )

    # ==========================================================
    # Контракт BaseDeploymentAdapter: deploy
    # ==========================================================

    def deploy(self, ticket) -> DeploymentActionResult:
        """
        Устанавливает пакет в новый каталог releases.

        Существующая версия не перезаписывается.
        После успешной распаковки переключается current.json.
        """

        preflight = self.preflight_package(ticket)

        self.root.mkdir(parents=True, exist_ok=True)
        self.releases_root.mkdir(parents=True, exist_ok=True)

        release_id = f"{self.RELEASE_ID_PREFIX}{uuid.uuid4().hex}"

        staging_path = (
            self.releases_root / f".staging-{uuid.uuid4().hex}"
        )
        final_release_path = self.releases_root / release_id

        try:
            staging_path.mkdir(parents=False, exist_ok=False)

            with ticket.package_file.open("rb") as source:
                with zipfile.ZipFile(source, "r") as archive:
                    self._validate_archive(
                        archive,
                        expected_version=preflight.version,
                    )

                    self._extract_archive(
                        archive,
                        staging_path,
                    )

            self._write_release_metadata(
                staging_path,
                release_id=release_id,
                version=preflight.version,
            )

            # staging и releases находятся на одной файловой системе.
            # Переименование каталога не затрагивает активную версию.
            os.replace(staging_path, final_release_path)

            self._write_current_pointer(
                release_id=release_id,
                version=preflight.version,
            )

            inspection = self.inspect()

            if (
                not inspection.installed
                or inspection.release_id != release_id
                or inspection.current_version != preflight.version
            ):
                raise DeploymentAdapterError(
                    "После установки проверка активной версии не пройдена."
                )

        except DeploymentAdapterError:
            raise
        except (
            OSError,
            zipfile.BadZipFile,
            RuntimeError,
            ValueError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось выполнить развёртывание пакета."
            ) from exc
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path, ignore_errors=True)

        return DeploymentActionResult(
            target_id=str(self.target.pk),
            version=preflight.version,
            release_id=release_id,
            message="Новая версия успешно развёрнута в локальной песочнице.",
        )
    
    def initialize_from_package(
        self,
        package_path: str | Path,
        *,
        expected_version: str | None = None,
    ) -> DeploymentActionResult:
        """
        Первоначально устанавливает пакет в пустое окружение.

        Используется только для подготовки лабораторной среды.
        Не заменяет обычный процесс обновления.
        """

        raw_path = Path(package_path).expanduser()

        if raw_path.is_symlink():
            raise DeploymentAdapterError(
                "ZIP-пакет не должен быть символической ссылкой."
            )

        try:
            source_path = raw_path.resolve(strict=True)
        except OSError as exc:
            raise DeploymentAdapterError(
                "ZIP-пакет не найден."
            ) from exc

        if not source_path.is_file():
            raise DeploymentAdapterError(
                "Указанный путь не является файлом."
            )

        # Не допускаем повторную первоначальную установку
        # поверх уже установленной версии.
        if (
            self.current_pointer.exists()
            or self.current_pointer.is_symlink()
        ):
            raise DeploymentAdapterError(
                "В целевом окружении уже существует current.json. "
                "Первоначальная установка отменена."
            )

        # Не используем каталог с остатками предыдущей установки.
        if (
            self.releases_root.exists()
            and any(self.releases_root.iterdir())
        ):
            raise DeploymentAdapterError(
                "Каталог releases уже содержит файлы. "
                "Проверьте целевое окружение перед установкой."
            )

        self.root.mkdir(parents=True, exist_ok=True)
        self.releases_root.mkdir(parents=True, exist_ok=True)

        release_id = f"{self.RELEASE_ID_PREFIX}{uuid.uuid4().hex}"

        staging_path = (
            self.releases_root / f".bootstrap-{uuid.uuid4().hex}"
        )
        final_release_path = self.releases_root / release_id

        published = False

        try:
            staging_path.mkdir(parents=False, exist_ok=False)

            with source_path.open("rb") as source:
                with zipfile.ZipFile(source, "r") as archive:
                    manifest, _ = self._validate_archive(
                        archive,
                        expected_version=expected_version,
                    )

                    version = manifest["version"]

                    self._extract_archive(
                        archive,
                        staging_path,
                    )

            self._write_release_metadata(
                staging_path,
                release_id=release_id,
                version=version,
            )

            os.replace(staging_path, final_release_path)

            self._write_current_pointer(
                release_id=release_id,
                version=version,
            )

            published = True

            inspection = self.inspect()

            if (
                not inspection.installed
                or inspection.release_id != release_id
                or inspection.current_version != version
            ):
                raise DeploymentAdapterError(
                    "Первоначальная установка завершилась, "
                    "но проверка активной версии не пройдена."
                )

        except DeploymentAdapterError:
            raise
        except (
            OSError,
            zipfile.BadZipFile,
            RuntimeError,
            ValueError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось выполнить первоначальную установку."
            ) from exc
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path, ignore_errors=True)

            # Удаляем незавершённый релиз, если указатель
            # активной версии ещё не был опубликован.
            if not published and final_release_path.exists():
                shutil.rmtree(final_release_path, ignore_errors=True)

        return DeploymentActionResult(
            target_id=str(self.target.pk),
            version=version,
            release_id=release_id,
            message=(
                "Первоначальная версия успешно установлена "
                "в лабораторное окружение."
            ),
        )


    # ==========================================================
    # Контракт BaseDeploymentAdapter: health_check
    # ==========================================================

    def health_check(
        self,
        expected_version: str | None = None,
    ) -> HealthCheckResult:
        """
        Выполняет файловую проверку установленной версии.

        Это пока не проверка работающего процесса приложения.
        Для неё позднее понадобится отдельный механизм проверки
        конкретного музейного ПО.
        """

        try:
            inspection = self.inspect()
        except DeploymentAdapterError as exc:
            return HealthCheckResult(
                ok=False,
                version=None,
                message=f"Ошибка проверки окружения: {exc}",
            )

        if not inspection.installed:
            return HealthCheckResult(
                ok=False,
                version=None,
                message="В целевом окружении нет установленной версии.",
            )

        if (
            expected_version is not None
            and inspection.current_version != expected_version
        ):
            return HealthCheckResult(
                ok=False,
                version=inspection.current_version,
                message="Установленная версия не совпадает с ожидаемой.",
            )

        if self.healthcheck_file:
            release_path = Path(inspection.release_path)

            check_path = (
                release_path
                / PurePosixPath(self.healthcheck_file)
            )

            if (
                not check_path.resolve().is_relative_to(release_path.resolve())
                or check_path.is_symlink()
                or not check_path.is_file()
            ):
                return HealthCheckResult(
                    ok=False,
                    version=inspection.current_version,
                    message=(
                        "Файл, указанный в healthcheck_file, "
                        "не найден или недоступен."
                    ),
                )

            return HealthCheckResult(
                ok=True,
                version=inspection.current_version,
                message="Файловая проверка healthcheck_file пройдена.",
            )

        return HealthCheckResult(
            ok=True,
            version=inspection.current_version,
            message=(
                "Файловая проверка пройдена: указатель и метаданные "
                "активной версии корректны. Проверка работающего "
                "процесса приложения пока не настроена."
            ),
        )

    # ==========================================================
    # Создание архива текущей версии
    # ==========================================================

    def _get_backup_temp_root(self) -> Path:
        configured = getattr(
            settings,
            "DEPLOYMENT_BACKUP_TEMP_ROOT",
            None,
        )

        if configured:
            temp_root = Path(configured).resolve()
        else:
            temp_root = (
                Path(tempfile.gettempdir())
                / "regulations_app_deployment_backups"
            ).resolve()

        if self._paths_overlap(self.root, temp_root):
            raise DeploymentConfigurationError(
                "Временный каталог резервной копии не должен "
                "пересекаться с каталогом целевого окружения."
            )

        temp_root.mkdir(parents=True, exist_ok=True)

        return temp_root

    def _add_release_files_to_backup(
        self,
        archive: zipfile.ZipFile,
        release_path: Path,
    ) -> None:
        file_count = 0
        total_size = 0

        for current_root, directory_names, file_names in os.walk(
            release_path,
            followlinks=False,
        ):
            current_path = Path(current_root)

            for directory_name in list(directory_names):
                directory_path = current_path / directory_name

                if directory_path.is_symlink():
                    raise DeploymentAdapterError(
                        "В активной версии обнаружена символическая ссылка."
                    )

            directory_names.sort()
            file_names.sort()

            for file_name in file_names:
                file_path = current_path / file_name

                if file_path.is_symlink() or not file_path.is_file():
                    raise DeploymentAdapterError(
                        "В активной версии обнаружен небезопасный файл."
                    )

                relative_path = file_path.relative_to(release_path)
                archive_name = relative_path.as_posix()

                if archive_name == self.RELEASE_METADATA_NAME:
                    continue

                try:
                    self._validate_relative_path(archive_name)
                except ValueError as exc:
                    raise DeploymentAdapterError(
                        "В активной версии обнаружено небезопасное имя файла."
                    ) from exc

                file_size = file_path.stat().st_size

                if file_size > self.archive_limits["max_file_size"]:
                    raise DeploymentAdapterError(
                        f"Файл слишком большой для резервной копии: "
                        f"{archive_name}."
                    )

                file_count += 1
                total_size += file_size

                if file_count > self.archive_limits["max_entries"]:
                    raise DeploymentAdapterError(
                        "В активной версии слишком много файлов."
                    )

                if total_size > self.archive_limits["max_total_size"]:
                    raise DeploymentAdapterError(
                        "Общий размер активной версии превышает лимит."
                    )

                archive.write(
                    file_path,
                    arcname=archive_name,
                )

    # ==========================================================
    # Контракт BaseDeploymentAdapter: create_backup
    # ==========================================================

    def create_backup(
        self,
        operation_id,
        actor,
    ) -> Backup:
        """
        Создаёт ZIP резервной копии активной версии.

        Сохранение записи Backup и аудит выполняются уже существующим
        backups.services.create_backup().

        Важно: этот метод вызывается при выполнении первого шага
        BACKUP операции UPDATE, когда StepExecution уже IN_PROGRESS.
        """

        current = self._get_current_release()

        if current is None:
            raise DeploymentAdapterError(
                "Нельзя создать резервную копию: "
                "в целевом окружении ещё нет установленной версии."
            )

        pointer, release_path = current

        temp_root = self._get_backup_temp_root()

        file_descriptor, temp_file_name = tempfile.mkstemp(
            prefix="deployment-backup-",
            suffix=".zip",
            dir=temp_root,
        )

        os.close(file_descriptor)

        temp_file_path = Path(temp_file_name)

        try:
            with zipfile.ZipFile(
                temp_file_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                self._add_release_files_to_backup(
                    archive,
                    release_path,
                )

                backup_metadata = {
                    "schema_version": self.BACKUP_SCHEMA_VERSION,
                    "target_id": str(self.target.pk),
                    "version": pointer["version"],
                    "release_id": pointer["release_id"],
                    "created_at": timezone.now().isoformat(),
                }

                archive.writestr(
                    self.BACKUP_METADATA_NAME,
                    json.dumps(
                        backup_metadata,
                        ensure_ascii=False,
                        indent=2,
                    ),
                )

            with temp_file_path.open("rb") as source:
                backup_file = DjangoFile(
                    source,
                    name=f"deployment-{uuid.uuid4().hex}.zip",
                )

                backup = create_backup_record(
                    operation_id=operation_id,
                    actor=actor,
                    file=backup_file,
                )

            return backup

        except DeploymentAdapterError:
            raise
        except Exception as exc:
            raise DeploymentAdapterError(
                "Не удалось создать резервную копию активной версии."
            ) from exc
        finally:
            temp_file_path.unlink(missing_ok=True)

    # ==========================================================
    # Контракт BaseDeploymentAdapter: restore
    # ==========================================================

    def restore(
        self,
        backup_id,
        actor,
    ) -> DeploymentActionResult:
        """
        Проверяет резервную копию, распаковывает её
        в новый каталог releases и переключает current.json.

        Исходная резервная копия не изменяется.
        """

        try:
            backup = verify_backup(
                backup_id=backup_id,
                actor=actor,
            )
        except Exception as exc:
            raise DeploymentAdapterError(
                "Не удалось выполнить проверку резервной копии."
            ) from exc

        if not backup.is_usable:
            raise DeploymentAdapterError(
                "Резервная копия не прошла проверку целостности."
            )

        # Резервная копия должна принадлежать операции
        # именно этого целевого окружения.
        backup_target_id = (
            backup.operation.ticket.deployment_target_id
        )

        if backup_target_id != self.target.pk:
            raise DeploymentAdapterError(
                "Резервная копия относится к другому целевому окружению."
            )

        release_id = f"{self.RELEASE_ID_PREFIX}{uuid.uuid4().hex}"

        staging_path = (
            self.releases_root / f".restore-{uuid.uuid4().hex}"
        )
        final_release_path = self.releases_root / release_id

        version = None

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.releases_root.mkdir(parents=True, exist_ok=True)
            staging_path.mkdir(parents=False, exist_ok=False)

            with backup.file.open("rb") as source:
                with zipfile.ZipFile(source, "r") as archive:
                    manifest, backup_metadata = self._validate_archive(
                        archive,
                        require_backup_metadata=True,
                    )

                    if backup_metadata is None:
                        raise DeploymentAdapterError(
                            "Отсутствует метадата резервной копии."
                        )

                    if backup_metadata.get("schema_version") != self.BACKUP_SCHEMA_VERSION:
                        raise DeploymentAdapterError(
                            "Неподдерживаемая версия формата резервной копии."
                        )

                    if backup_metadata.get("target_id") != str(self.target.pk):
                        raise DeploymentAdapterError(
                            "Резервная копия создана для другого окружения."
                        )

                    version = manifest["version"]

                    if backup_metadata.get("version") != version:
                        raise DeploymentAdapterError(
                            "Версия в метадате резервной копии "
                            "не совпадает с manifest.json."
                        )

                    self._extract_archive(
                        archive,
                        staging_path,
                        skip_root_names={
                            self.BACKUP_METADATA_NAME,
                        },
                    )

            self._write_release_metadata(
                staging_path,
                release_id=release_id,
                version=version,
            )

            os.replace(staging_path, final_release_path)

            self._write_current_pointer(
                release_id=release_id,
                version=version,
            )

            inspection = self.inspect()

            if (
                not inspection.installed
                or inspection.release_id != release_id
                or inspection.current_version != version
            ):
                raise DeploymentAdapterError(
                    "После восстановления проверка активной версии не пройдена."
                )

        except DeploymentAdapterError:
            raise
        except (
            OSError,
            zipfile.BadZipFile,
            RuntimeError,
            ValueError,
        ) as exc:
            raise DeploymentAdapterError(
                "Не удалось восстановить версию из резервной копии."
            ) from exc
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path, ignore_errors=True)

        return DeploymentActionResult(
            target_id=str(self.target.pk),
            version=version,
            release_id=release_id,
            message=(
                "Версия успешно восстановлена из резервной копии "
                "в локальной песочнице."
            ),
        )
