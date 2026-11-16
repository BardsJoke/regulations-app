import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from access.models import ModuleRole, RoleAssignment
from backups.models import Backup
from catalogs.models import CardStatusCode
from deployments.adapters.registry import get_deployment_adapter

from .models import (
    ChangeTicket,
    CompatibilityStatus,
    ResourcesCheckStatus,
)


class PreflightError(ValidationError):
    """Ошибка предварительной проверки карточки изменения."""


@dataclass(frozen=True)
class PreflightReport:
    """Результат предварительной проверки."""

    compatibility_status: str
    resources_status: str
    messages: tuple[str, ...]

    @property
    def can_start(self):
        return (
            self.compatibility_status == CompatibilityStatus.COMPATIBLE
            and self.resources_status == ResourcesCheckStatus.SUFFICIENT
        )


def _require_executor(actor):
    """Проверяет права исполнителя."""

    if (
        not actor
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
    ):
        raise PermissionDenied(
            "Для предварительной проверки необходимо войти в систему."
        )

    if not RoleAssignment.objects.filter(
        user_id=actor.pk,
        role=ModuleRole.EXECUTOR,
    ).exists():
        raise PermissionDenied(
            "Предварительную проверку может выполнять только исполнитель."
        )


def _error_text(error):
    """Преобразует исключение в понятное сообщение."""

    if isinstance(error, ValidationError):
        errors = getattr(error, "messages", None)

        if errors:
            return "; ".join(str(item) for item in errors)

    return str(error).strip() or type(error).__name__


def _snapshot_ticket(ticket):
    """
    Сохраняет существенные параметры карточки,
    чтобы обнаружить их изменение во время проверки.
    """

    return {
        "created_by_id": ticket.created_by_id,
        "status_id": ticket.status_id,
        "software_id": ticket.software_id,
        "software_package_id": ticket.software_package_id,
        "deployment_target_id": ticket.deployment_target_id,
        "source_version": ticket.source_version,
        "target_version": ticket.target_version,
        "expected_sha256": ticket.expected_sha256,
        "package_file": ticket.package_file.name if ticket.package_file else "",
    }


def _check_ticket_access(ticket, actor):
    """Проверяет владельца и статус карточки."""

    if ticket.created_by_id != actor.pk:
        raise PermissionDenied(
            "Исполнитель может проверять только собственные карточки."
        )

    if not ticket.status_id:
        raise PreflightError(
            "У карточки не указан статус."
        )

    if ticket.status.code not in {
        CardStatusCode.APPROVED,
        CardStatusCode.IN_PROGRESS,
    }:
        raise PreflightError(
            "Предварительная проверка допустима только "
            "для согласованной карточки или повторной попытки."
        )

    if not ticket.approved_by_id:
        raise PreflightError(
            "Карточка не имеет подтверждения согласования."
        )


def _check_catalog_relationships(ticket):
    """
    Проверяет соответствие программного обеспечения,
    пакета и целевой установки.
    """

    errors = []

    software = ticket.software
    package = ticket.software_package
    target = ticket.deployment_target

    if not software:
        errors.append(
            "В карточке не указано программное обеспечение."
        )

    elif not software.is_active:
        errors.append(
            "Программное обеспечение неактивно."
        )

    if not package:
        errors.append(
            "В карточке не выбран пакет обновления."
        )

    else:
        if not package.is_usable:
            errors.append(
                "Пакет обновления не имеет подтверждённого "
                "статуса проверки."
            )

        if package.software_id != ticket.software_id:
            errors.append(
                "Пакет относится к другому программному обеспечению."
            )

        if package.version != str(ticket.target_version):
            errors.append(
                "Версия пакета не совпадает с целевой версией карточки."
            )

        if (
            package.package_file
            and ticket.package_file
            and package.package_file.name != ticket.package_file.name
        ):
            errors.append(
                "Файл пакета в карточке не совпадает "
                "с файлом зарегистрированного пакета."
            )

        if (
            ticket.expected_sha256
            and package.sha256.lower()
            != ticket.expected_sha256.lower()
        ):
            errors.append(
                "SHA-256 в карточке не совпадает с SHA-256 "
                "зарегистрированного пакета."
            )

        manifest_version = (
            package.manifest.get("version")
            if isinstance(package.manifest, dict)
            else None
        )

        if manifest_version != package.version:
            errors.append(
                "Версия в сохранённом манифесте пакета "
                "не совпадает с версией пакета."
            )

    if not target:
        errors.append(
            "В карточке не выбрана целевая установка."
        )

    else:
        if target.software_id != ticket.software_id:
            errors.append(
                "Целевая установка относится к другому ПО."
            )

        if not target.is_active:
            errors.append(
                "Целевая установка неактивна."
            )

        if not target.is_configured:
            errors.append(
                "Целевая установка не настроена."
            )

        if not target.software.is_active:
            errors.append(
                "Программное обеспечение целевой установки неактивно."
            )

    source_version = str(ticket.source_version or "").strip()
    target_version = str(ticket.target_version or "").strip()

    if not source_version:
        errors.append(
            "В карточке не указана исходная версия."
        )

    if not target_version:
        errors.append(
            "В карточке не указана целевая версия."
        )

    if source_version and target_version:
        if source_version == target_version:
            errors.append(
                "Исходная и целевая версии совпадают. "
                "Обновление не требуется."
            )

    if not ticket.expected_sha256:
        errors.append(
            "В карточке отсутствует ожидаемая SHA-256."
        )

    return errors


def _directory_size(directory):
    """
    Рассчитывает размер файлов установленного релиза.

    Символические ссылки запрещены, поскольку адаптер
    резервного копирования также не допускает их.
    """

    root = Path(directory)

    if root.is_symlink() or not root.is_dir():
        raise PreflightError(
            "Каталог установленной версии недоступен."
        )

    total_size = 0

    for current_root, directory_names, file_names in os.walk(
        root,
        followlinks=False,
    ):
        current_path = Path(current_root)

        for directory_name in directory_names:
            directory_path = current_path / directory_name

            if directory_path.is_symlink():
                raise PreflightError(
                    "В установленной версии обнаружена "
                    "символическая ссылка на каталог."
                )

        for file_name in file_names:
            file_path = current_path / file_name

            if file_path.is_symlink() or not file_path.is_file():
                raise PreflightError(
                    "В установленной версии обнаружен "
                    "небезопасный файл."
                )

            total_size += file_path.stat().st_size

    return total_size


def _existing_directory(path):
    """
    Возвращает ближайший существующий каталог для проверки
    доступного места на файловой системе.
    """

    candidate = Path(path).expanduser()

    while not candidate.exists():
        parent = candidate.parent

        if parent == candidate:
            raise PreflightError(
                "Не удалось определить существующий каталог "
                "для проверки свободного места."
            )

        candidate = parent

    if candidate.is_file():
        candidate = candidate.parent

    if candidate.is_symlink():
        raise PreflightError(
            "Каталог проверки свободного места "
            "не должен быть символической ссылкой."
        )

    return candidate.resolve()


def _filesystem_key(path):
    """Возвращает идентификатор файловой системы."""

    path = _existing_directory(path)

    if os.name == "nt":
        drive = os.path.splitdrive(str(path))[0]

        if not drive:
            raise PreflightError(
                "Не удалось определить диск Windows для проверки ресурсов."
            )

        return f"drive:{drive.casefold()}"

    try:
        device = os.stat(path).st_dev
    except OSError as error:
        raise PreflightError(
            "Не удалось определить файловую систему."
        ) from error

    return f"device:{device}"


def _check_disk_space(
    *,
    adapter,
    inspection,
    package_preflight,
):
    """
    Проверяет свободное место на всех файловых системах,
    необходимых для установки и резервного копирования.

    Учитываются:

    - распакованный пакет в каталоге целевой установки;
    - временный ZIP резервной копии;
    - сохранённый ZIP резервной копии в хранилище MEDIA.
    """

    if not inspection.release_path:
        raise PreflightError(
            "Невозможно рассчитать размер текущей версии "
            "для резервного копирования."
        )

    backup_size = _directory_size(
        inspection.release_path
    )

    if backup_size <= 0:
        raise PreflightError(
            "Текущая версия не содержит файлов "
            "для резервного копирования."
        )

    # Используем тот же временный каталог,
    # который использует сам адаптер при создании копии.
    temp_backup_root = adapter._get_backup_temp_root()

    # Получаем реальное хранилище поля Backup.file.
    backup_storage = Backup._meta.get_field("file").storage

    try:
        media_backup_root = Path(
            backup_storage.path("backups")
        )
    except (NotImplementedError, AttributeError) as error:
        raise PreflightError(
            "Невозможно определить локальное хранилище "
            "резервных копий. Проверка ресурсов не завершена."
        ) from error

    requirements = [
        (
            adapter.root,
            package_preflight.uncompressed_size_bytes,
            "Установка новой версии",
        ),
        (
            temp_backup_root,
            backup_size,
            "Временный файл резервной копии",
        ),
        (
            media_backup_root,
            backup_size,
            "Хранение резервной копии",
        ),
    ]

    volumes = {}

    for location, required_bytes, description in requirements:
        path = _existing_directory(location)
        volume_key = _filesystem_key(path)

        if volume_key not in volumes:
            volumes[volume_key] = {
                "path": path,
                "required": 0,
                "descriptions": [],
            }

        volumes[volume_key]["required"] += required_bytes
        volumes[volume_key]["descriptions"].append(description)

    results = []
    insufficient = False

    for volume_key, data in volumes.items():
        path = data["path"]
        required = data["required"]

        usage = shutil.disk_usage(path)

        # Добавляем 10% запаса и минимум 1 МБ.
        safety_margin = max(
            1024 * 1024,
            math.ceil(required * 0.10),
        )

        total_required = required + safety_margin
        available = usage.free

        if available < total_required:
            insufficient = True

            results.append(
                f"Недостаточно места на {path}. "
                f"Требуется не менее {total_required} байт "
                f"с учётом запаса, доступно {available} байт. "
                f"Операции: {', '.join(data['descriptions'])}."
            )

        else:
            results.append(
                f"Достаточно места на {path}. "
                f"Требуется {total_required} байт, "
                f"доступно {available} байт."
            )

    return not insufficient, results


def run_preflight(ticket_id, actor):
    """
    Выполняет предварительную проверку карточки изменения.

    Проверяет:
    - права исполнителя;
    - владельца и статус карточки;
    - согласованность ПО, пакета и целевой установки;
    - активность и конфигурацию стенда;
    - текущую установленную версию;
    - ZIP, manifest.json и SHA-256 пакета;
    - достаточность места для установки и резервного копирования.

    Результаты сохраняются в карточке.

    При отрицательном результате операция не создаётся.
    """

    _require_executor(actor)

    try:
        ticket = (
            ChangeTicket.objects
            .select_related(
                "status",
                "software",
                "software_package",
                "deployment_target",
                "deployment_target__software",
            )
            .get(pk=ticket_id)
        )
    except ChangeTicket.DoesNotExist:
        raise PreflightError(
            "Карточка изменения не найдена."
        )

    _check_ticket_access(ticket, actor)

    initial_snapshot = _snapshot_ticket(ticket)

    compatibility_errors = []
    resource_messages = []

    package_preflight = None
    inspection = None
    adapter = None

    # --------------------------------------------------------
    # 1. Проверяем связи и метаданные.
    # --------------------------------------------------------

    compatibility_errors.extend(
        _check_catalog_relationships(ticket)
    )

    # --------------------------------------------------------
    # 2. Получаем адаптер.
    # --------------------------------------------------------

    target = ticket.deployment_target

    if target and target.can_execute:
        try:
            adapter = get_deployment_adapter(target)
        except Exception as error:
            compatibility_errors.append(
                "Не удалось получить адаптер целевой установки: "
                + _error_text(error)
            )

    elif target:
        compatibility_errors.append(
            "Целевая установка не готова к выполнению операций."
        )

    # --------------------------------------------------------
    # 3. Проверяем фактически установленную версию.
    # --------------------------------------------------------

    if adapter is not None:
        try:
            inspection = adapter.inspect()

            if not inspection.installed:
                compatibility_errors.append(
                    "На целевой установке отсутствует "
                    "исходная версия программного обеспечения."
                )

            elif (
                inspection.current_version
                != str(ticket.source_version)
            ):
                compatibility_errors.append(
                    "Фактическая версия на стенде не совпадает "
                    "с исходной версией карточки. "
                    f"Ожидается: {ticket.source_version}; "
                    f"обнаружена: {inspection.current_version}."
                )

        except Exception as error:
            compatibility_errors.append(
                "Не удалось проверить текущую установку: "
                + _error_text(error)
            )

    # --------------------------------------------------------
    # 4. Проверяем реальный ZIP-пакет.
    # --------------------------------------------------------

    if adapter is not None and ticket.package_file:
        try:
            package_preflight = adapter.preflight_package(ticket)

            package = ticket.software_package

            if package:
                if (
                    package_preflight.sha256.lower()
                    != package.sha256.lower()
                ):
                    compatibility_errors.append(
                        "Фактическая SHA-256 файла "
                        "не совпадает с SHA-256 каталога пакетов."
                    )

                if package_preflight.size_bytes != package.size_bytes:
                    compatibility_errors.append(
                        "Фактический размер файла не совпадает "
                        "с размером, сохранённым в каталоге пакетов."
                    )

                if package_preflight.version != package.version:
                    compatibility_errors.append(
                        "Версия в ZIP-пакете не совпадает "
                        "с версией зарегистрированного пакета."
                    )

        except Exception as error:
            compatibility_errors.append(
                "Предварительная проверка пакета не пройдена: "
                + _error_text(error)
            )

    # --------------------------------------------------------
    # 5. Проверяем свободное место.
    # --------------------------------------------------------

    resources_status = ResourcesCheckStatus.NOT_CHECKED

    if (
        adapter is not None
        and inspection is not None
        and inspection.installed
        and package_preflight is not None
    ):
        try:
            enough_space, resource_messages = _check_disk_space(
                adapter=adapter,
                inspection=inspection,
                package_preflight=package_preflight,
            )

            if enough_space:
                resources_status = ResourcesCheckStatus.SUFFICIENT
            else:
                resources_status = ResourcesCheckStatus.INSUFFICIENT

        except Exception as error:
            resource_messages.append(
                "Не удалось достоверно проверить доступное место: "
                + _error_text(error)
            )

    else:
        resource_messages.append(
            "Проверка ресурсов не завершена: "
            "не удалось получить данные пакета или текущей установки."
        )

    # --------------------------------------------------------
    # 6. Определяем результаты проверки совместимости.
    # --------------------------------------------------------

    if compatibility_errors:
        compatibility_status = CompatibilityStatus.INCOMPATIBLE
    else:
        compatibility_status = CompatibilityStatus.COMPATIBLE

    all_messages = (
        compatibility_errors
        + resource_messages
    )

    if not all_messages:
        all_messages.append(
            "Совместимость и достаточность ресурсов подтверждены."
        )

    # --------------------------------------------------------
    # 7. Сохраняем результаты.
    # --------------------------------------------------------

    with transaction.atomic():
        locked_ticket = (
            ChangeTicket.objects
            .select_for_update()
            .get(pk=ticket.pk)
        )

        current_snapshot = _snapshot_ticket(
            locked_ticket
        )

        if current_snapshot != initial_snapshot:
            raise PreflightError(
                "Параметры карточки изменились во время проверки. "
                "Повторите предварительную проверку."
            )

        locked_ticket.compatibility_status = compatibility_status
        locked_ticket.resources_check_status = resources_status

        locked_ticket.save(
            update_fields=[
                "compatibility_status",
                "resources_check_status",
                "updated_at",
            ]
        )

    return PreflightReport(
        compatibility_status=compatibility_status,
        resources_status=resources_status,
        messages=tuple(all_messages),
    )