from django.core.exceptions import ValidationError

from backups.models import Backup
from backups.services import verify_backup
from changes.models import StepKind
from deployments.adapters.registry import get_deployment_adapter

from .models import (
    OperationType,
    StepExecution,
)
from .services import (
    OperationServiceError,
    start_step,
    complete_step,
    fail_step,
)


def _get_error_text(error):
    """
    Преобразует исключение в понятное текстовое сообщение.
    """

    if isinstance(error, ValidationError):
        messages = getattr(error, "messages", None)

        if messages:
            return "; ".join(str(message) for message in messages)

    return str(error).strip() or type(error).__name__


def _get_step(step_execution_id):
    """
    Получает шаг вместе с операцией, карточкой и целевой установкой.
    """

    try:
        return (
            StepExecution.objects
            .select_related(
                "operation",
                "operation__change_ticket",
                "operation__change_ticket__deployment_target",
                "operation__backup_used",
            )
            .get(pk=step_execution_id)
        )
    except StepExecution.DoesNotExist:
        raise OperationServiceError(
            "Шаг операции не найден."
        )


def _get_adapter(ticket):
    """
    Получает адаптер целевой установки.
    """

    if not ticket.deployment_target_id:
        raise OperationServiceError(
            "В карточке изменения не указана целевая установка."
        )

    return get_deployment_adapter(ticket.deployment_target)


def _check_source_version(adapter, ticket):
    """
    Проверяет, что на стенде установлена именно исходная версия
    из карточки изменения.
    """

    inspection = adapter.inspect()

    if not inspection.installed:
        raise OperationServiceError(
            "На целевой установке отсутствует исходная версия ПО."
        )

    expected_version = str(ticket.source_version)

    if inspection.current_version != expected_version:
        raise OperationServiceError(
            "Версия на целевой установке не совпадает "
            f"с исходной версией карточки. "
            f"Ожидается {expected_version}, "
            f"обнаружена {inspection.current_version}."
        )

    return inspection


def _verify_operation_backup(operation, actor):
    """
    Находит резервную копию текущей операции
    и повторно проверяет её целостность.
    """

    backup = (
        Backup.objects
        .filter(operation=operation)
        .order_by("-created_at")
        .first()
    )

    if backup is None:
        raise OperationServiceError(
            "Для операции не найдена резервная копия."
        )

    verified_backup = verify_backup(
        backup_id=backup.pk,
        actor=actor,
    )

    if not verified_backup.is_usable:
        raise OperationServiceError(
            "Резервная копия не прошла проверку целостности. "
            "Продолжение обновления запрещено."
        )

    return verified_backup


# ============================================================
# АВТОМАТИЧЕСКИЕ ДЕЙСТВИЯ ПРИ ОБНОВЛЕНИИ
# ============================================================

def _execute_backup(operation, ticket, adapter, actor):
    """
    Создаёт и проверяет резервную копию исходной версии.
    """

    inspection = _check_source_version(adapter, ticket)

    backup = adapter.create_backup(
        operation_id=operation.pk,
        actor=actor,
    )

    verified_backup = verify_backup(
        backup_id=backup.pk,
        actor=actor,
    )

    if not verified_backup.is_usable:
        raise OperationServiceError(
            "Резервная копия создана, но не прошла проверку целостности."
        )

    return (
        f"Резервная копия создана и проверена. "
        f"Версия: {inspection.current_version}. "
        f"ID копии: {verified_backup.pk}. "
        f"SHA-256: {verified_backup.file_sha256}. "
        f"Размер: {verified_backup.size_bytes} байт."
    )


def _execute_update(operation, ticket, adapter, actor):
    """
    Устанавливает пакет обновления.

    Перед установкой проверяет исходную версию,
    наличие резервной копии, целостность пакета
    и результат предварительной проверки.
    """

    # Проверяем, что исходная версия не изменилась.
    inspection = _check_source_version(adapter, ticket)

    # Проверяем резервную копию перед обновлением.
    backup = _verify_operation_backup(
        operation,
        actor,
    )

    # Проверяем ZIP, SHA-256 и целевую версию.
    preflight = adapter.preflight_package(ticket)

    if preflight.version != str(ticket.target_version):
        raise OperationServiceError(
            "Версия пакета после предварительной проверки "
            "не совпадает с целевой версией карточки."
        )

    # Физическая установка новой версии.
    deployment_result = adapter.deploy(ticket)

    # Проверяем результат установки.
    health_result = adapter.health_check(
        expected_version=str(ticket.target_version),
    )

    if not health_result.ok:
        raise OperationServiceError(
            "Пакет был передан адаптеру для установки, "
            "но итоговая проверка не пройдена. "
            f"Резервная копия {backup.pk} сохранена. "
            f"Результат проверки: {health_result.message}"
        )

    return (
        f"Обновление успешно выполнено. "
        f"Исходная версия: {inspection.current_version}. "
        f"Новая версия: {deployment_result.version}. "
        f"Релиз: {deployment_result.release_id}. "
        f"Проверка: {health_result.message}. "
        f"Резервная копия: {backup.pk}."
    )


# ============================================================
# АВТОМАТИЧЕСКИЕ ДЕЙСТВИЯ ПРИ ОТКАТЕ
# ============================================================

def _execute_restore(operation, adapter, actor):
    """
    Восстанавливает установку из резервной копии,
    выбранной при создании операции отката.
    """

    if not operation.backup_used_id:
        raise OperationServiceError(
            "Для операции отката не указана резервная копия."
        )

    restore_result = adapter.restore(
        backup_id=operation.backup_used_id,
        actor=actor,
    )

    health_result = adapter.health_check(
        expected_version=restore_result.version,
    )

    if not health_result.ok:
        raise OperationServiceError(
            "Восстановление было выполнено, "
            "но проверка восстановленной версии не пройдена. "
            f"Результат: {health_result.message}"
        )

    return (
        f"Восстановление из резервной копии выполнено. "
        f"Версия: {restore_result.version}. "
        f"Релиз: {restore_result.release_id}. "
        f"Проверка: {health_result.message}"
    )


# ============================================================
# ОТДЕЛЬНЫЙ ШАГ ПРОВЕРКИ
# ============================================================

def _execute_verification(operation, ticket, adapter):
    """
    Выполняет проверку установленной версии.

    При обновлении ожидается target_version.
    При откате ожидается source_version.
    """

    if operation.operation_type == OperationType.UPDATE:
        expected_version = str(ticket.target_version)

    else:
        expected_version = str(ticket.source_version)

    health_result = adapter.health_check(
        expected_version=expected_version,
    )

    if not health_result.ok:
        raise OperationServiceError(
            "Проверка целевой установки завершилась неудачно: "
            f"{health_result.message}"
        )

    return (
        f"Проверка успешно завершена. "
        f"Версия: {health_result.version}. "
        f"Результат: {health_result.message}"
    )


# ============================================================
# ВЫБОР АВТОМАТИЧЕСКОГО ДЕЙСТВИЯ
# ============================================================

def _perform_automatic_action(step, actor):
    """
    Определяет действие по типу операции и типу шага.
    """

    operation = step.operation
    ticket = operation.change_ticket

    adapter = _get_adapter(ticket)

    if operation.operation_type == OperationType.UPDATE:

        if step.step_kind == StepKind.BACKUP:
            return _execute_backup(
                operation,
                ticket,
                adapter,
                actor,
            )

        if step.step_kind == StepKind.UPDATE:
            return _execute_update(
                operation,
                ticket,
                adapter,
                actor,
            )

        if step.step_kind == StepKind.VERIFICATION:
            return _execute_verification(
                operation,
                ticket,
                adapter,
            )

    elif operation.operation_type == OperationType.ROLLBACK:

        if step.step_kind == StepKind.UPDATE:
            return _execute_restore(
                operation,
                adapter,
                actor,
            )

        if step.step_kind == StepKind.VERIFICATION:
            return _execute_verification(
                operation,
                ticket,
                adapter,
            )

    raise OperationServiceError(
        "Для этого типа операции и шага "
        "не настроено автоматическое действие. "
        "Возможно, шаг предназначен для ручного выполнения."
    )


# ============================================================
# ПУБЛИЧНАЯ ФУНКЦИЯ ВЫПОЛНЕНИЯ ШАГА
# ============================================================

def execute_automatic_step(step_execution_id, actor):
    """
    Полностью выполняет один автоматический шаг.

    Последовательность:

    1. Получает фактический шаг.
    2. Переводит его в IN_PROGRESS через существующий сервис.
    3. Выполняет физическое действие адаптера.
    4. При успехе завершает шаг.
    5. При ошибке переводит шаг и операцию в FAILED.

    Физическое действие выполняется вне транзакции изменения
    статусов, чтобы не держать блокировки БД во время работы с файлами.
    """

    step = _get_step(step_execution_id)

    # Шаг OTHER предназначен для ручного выполнения.
    # Его нельзя автоматически подтверждать.
    if step.step_kind == StepKind.OTHER:
        raise OperationServiceError(
            "Этот шаг предназначен для ручного выполнения. "
            "Используйте обычные действия начала и завершения шага."
        )

    # Начало шага. Существующий сервис проверяет права,
    # последовательность и состояние операции.
    started_step = start_step(
        step_execution_id=step_execution_id,
        actor=actor,
    )

    # Физическое действие адаптера выполняется после фиксации
    # состояния IN_PROGRESS.
    try:
        result_text = _perform_automatic_action(
            step=started_step,
            actor=actor,
        )

    except Exception as error:
        error_text = _get_error_text(error)

        # Фиксируем ошибку в существующем сервисе операций.
        try:
            fail_step(
                step_execution_id=step_execution_id,
                actor=actor,
                error_message=error_text,
            )
        except Exception as fail_error:
            raise OperationServiceError(
                f"Ошибка выполнения шага: {error_text}. "
                "Дополнительно не удалось сохранить ошибку "
                f"в операции: {_get_error_text(fail_error)}. "
                "Не повторяйте физическое действие до проверки "
                "состояния стенда."
            ) from fail_error

        raise OperationServiceError(
            f"Шаг «{started_step.name}» завершился ошибкой: "
            f"{error_text}"
        ) from error

    # Физическое действие прошло успешно.
    # Теперь сохраняем результат и открываем следующий шаг.
    try:
        return complete_step(
            step_execution_id=step_execution_id,
            actor=actor,
            result=result_text,
        )

    except Exception as error:
        raise OperationServiceError(
            "Физическое действие завершилось успешно, "
            "но не удалось зафиксировать завершение шага в базе данных. "
            "Перед повторным запуском обязательно проверьте состояние "
            "целевой установки и записи операции."
        ) from error