
import hashlib
import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment
from audit.services import AuditAction, record_event
from operations.models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
)

from .models import Backup, BackupIntegrityStatus


logger = logging.getLogger(__name__)


class BackupServiceError(ValidationError):
    """Ошибка при создании или проверке резервной копии."""
    pass


# ============================================================
# ПРОВЕРКА ПРАВ
# ============================================================

def _require_executor(actor):
    """Проверяет наличие активной роли исполнителя."""

    if (
        not actor
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
    ):
        raise PermissionDenied(
            "Необходима активная учётная запись."
        )

    has_role = RoleAssignment.objects.filter(
        user_id=actor.pk,
        role=ModuleRole.EXECUTOR,
    ).exists()

    if not has_role:
        raise PermissionDenied(
            "Создавать и проверять резервные копии "
            "может только исполнитель."
        )


# ============================================================
# ВЫЧИСЛЕНИЕ SHA-256 И РАЗМЕРА ФАЙЛА
# ============================================================

def _calculate_file_metadata(file_obj):
    """
    Вычисляет SHA-256 и размер файла потоковым способом.

    Одновременно весь файл в память не загружается.
    """

    digest = hashlib.sha256()
    total_size = 0

    try:
        file_obj.seek(0)
    except (AttributeError, OSError, ValueError):
        pass

    if hasattr(file_obj, "chunks"):
        chunks = file_obj.chunks()
    else:
        chunks = iter(
            lambda: file_obj.read(1024 * 1024),
            b"",
        )

    for chunk in chunks:
        digest.update(chunk)
        total_size += len(chunk)

    try:
        file_obj.seek(0)
    except (AttributeError, OSError, ValueError):
        pass

    return digest.hexdigest(), total_size


def _cleanup_backup_file(backup):
    """
    Пытается удалить файл, если создание записи не завершилось.

    Ошибка очистки не должна скрывать исходное исключение.
    """

    if not backup.file or not backup.file.name:
        return

    try:
        backup.file.storage.delete(backup.file.name)
    except Exception:
        logger.exception(
            "Не удалось удалить файл резервной копии "
            "после сбоя создания записи."
        )


# ============================================================
# СОЗДАНИЕ РЕЗЕРВНОЙ КОПИИ
# ============================================================

@transaction.atomic
def create_backup(*, operation_id, actor, file):
    """
    Создаёт резервную копию в рамках выполняющейся операции UPDATE.

    Копию разрешено создавать только во время выполнения
    первого фактического шага резервного копирования.
    """

    _require_executor(actor)

    if not file:
        raise BackupServiceError(
            "Необходимо передать файл резервной копии."
        )

    # 1. Получаем операцию с блокировкой.
    try:
        operation = (
            Operation.objects
            .select_for_update()
            .get(pk=operation_id)
        )
    except Operation.DoesNotExist:
        raise BackupServiceError(
            "Операция не найдена."
        )

    if operation.operation_type != OperationType.UPDATE:
        raise BackupServiceError(
            "Создавать резервную копию можно только "
            "для операции обновления."
        )

    if operation.status != OperationStatus.RUNNING:
        raise BackupServiceError(
            "Операция не находится в состоянии выполнения."
        )

    # 2. Проверяем первый фактический шаг.
    backup_step = (
        StepExecution.objects
        .select_for_update()
        .filter(
            operation=operation,
            step_order=1,
            step_kind="backup",
        )
        .first()
    )

    if backup_step is None:
        raise BackupServiceError(
            "В операции отсутствует шаг резервного копирования."
        )

    if backup_step.status != StepExecutionStatus.IN_PROGRESS:
        raise BackupServiceError(
            "Сначала необходимо начать шаг резервного копирования."
        )

    if backup_step.started_by_id != actor.pk:
        raise PermissionDenied(
            "Создать резервную копию может исполнитель, "
            "начавший шаг резервного копирования."
        )

    # 3. Вычисляем контрольную сумму и размер.
    try:
        file_sha256, size_bytes = _calculate_file_metadata(file)
    except (OSError, ValueError) as error:
        raise BackupServiceError(
            "Не удалось прочитать файл резервной копии."
        ) from error

    if size_bytes == 0:
        raise BackupServiceError(
            "Нельзя создавать пустую резервную копию."
        )

    # 4. Создаём запись.
    backup = Backup(
        operation=operation,
        file=file,
        file_sha256=file_sha256,
        size_bytes=size_bytes,
        created_by=actor,
        integrity_status=BackupIntegrityStatus.NOT_CHECKED,
    )

    backup.full_clean()

    try:
        backup.save()

        # 5. Регистрируем успешное создание в аудите.
        record_event(
            actor=actor,
            action=AuditAction.BACKUP_CREATED,
            obj=backup,
            details={
                "operation_id": str(operation.pk),
                "file_sha256": backup.file_sha256,
                "size_bytes": backup.size_bytes,
                "integrity_status": backup.integrity_status,
            },
        )

    except Exception:
        # Если файл успел сохраниться, но запись в БД
        # или аудит завершились ошибкой, удаляем файл.
        _cleanup_backup_file(backup)
        raise

    return backup


# ============================================================
# ПОВТОРНАЯ ПРОВЕРКА РЕЗЕРВНОЙ КОПИИ
# ============================================================

@transaction.atomic
def verify_backup(*, backup_id, actor):
    """
    Повторно вычисляет SHA-256 и размер сохранённого файла.

    Результат проверки записывается в Backup:
    VALID, INVALID или ERROR.

    Результат каждой завершённой проверки фиксируется в аудите.
    """

    _require_executor(actor)

    try:
        backup = (
            Backup.objects
            .select_for_update()
            .get(pk=backup_id)
        )
    except Backup.DoesNotExist:
        raise BackupServiceError(
            "Резервная копия не найдена."
        )

    verified_at = timezone.now()

    actual_sha256 = None
    actual_size = None

    try:
        with backup.file.open("rb") as stored_file:
            actual_sha256, actual_size = (
                _calculate_file_metadata(stored_file)
            )

    except Exception as error:
        # Не раскрываем внутренние пути и детали хранилища.
        backup.integrity_status = BackupIntegrityStatus.ERROR
        backup.verified_at = verified_at
        backup.verification_error = (
            "Не удалось прочитать резервную копию "
            f"({type(error).__name__})."
        )

    else:
        # Проверяем и SHA-256, и размер.
        if (
            actual_sha256 == backup.file_sha256
            and actual_size == backup.size_bytes
        ):
            backup.integrity_status = BackupIntegrityStatus.VALID
            backup.verification_error = ""

        else:
            backup.integrity_status = BackupIntegrityStatus.INVALID

            if actual_sha256 != backup.file_sha256:
                backup.verification_error = (
                    "SHA-256 файла не совпадает с сохранённой "
                    "контрольной суммой."
                )
            else:
                backup.verification_error = (
                    "Размер файла не совпадает с сохранённым значением."
                )

        backup.verified_at = verified_at

    # Сначала сохраняем фактический результат проверки.
    backup.full_clean()

    backup.save(
        update_fields=[
            "integrity_status",
            "verified_at",
            "verification_error",
            "updated_at",
        ]
    )

    # Затем фиксируем результат в аудите.
    if backup.integrity_status == BackupIntegrityStatus.VALID:
        record_event(
            actor=actor,
            action=AuditAction.BACKUP_VERIFIED,
            obj=backup,
            details={
                "integrity_status": backup.integrity_status,
                "file_sha256": backup.file_sha256,
                "actual_sha256": actual_sha256,
                "size_bytes": backup.size_bytes,
                "actual_size_bytes": actual_size,
            },
        )

    else:
        record_event(
            actor=actor,
            action=AuditAction.BACKUP_VERIFICATION_FAILED,
            obj=backup,
            details={
                "integrity_status": backup.integrity_status,
                "verification_error": backup.verification_error,
                "expected_sha256": backup.file_sha256,
                "actual_sha256": actual_sha256,
                "expected_size_bytes": backup.size_bytes,
                "actual_size_bytes": actual_size,
            },
        )

    return backup


# ============================================================
# ПОЛУЧЕНИЕ ПРИГОДНЫХ РЕЗЕРВНЫХ КОПИЙ
# ============================================================

def get_usable_backups(*, operation_id, actor):
    """
    Возвращает только резервные копии с подтверждённой целостностью.

    Перед фактическим откатом выбранная копия должна пройти
    дополнительную проверку.
    """

    _require_executor(actor)

    try:
        operation = Operation.objects.get(pk=operation_id)
    except Operation.DoesNotExist:
        raise BackupServiceError(
            "Операция не найдена."
        )

    if operation.operation_type != OperationType.UPDATE:
        raise BackupServiceError(
            "Резервные копии необходимо выбирать "
            "из исходной операции обновления."
        )

    return (
        Backup.objects
        .filter(
            operation=operation,
            integrity_status=BackupIntegrityStatus.VALID,
            verified_at__isnull=False,
        )
        .order_by("-created_at")
    )
