import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from backups.models import Backup
from backups.services import verify_backup
from changes.models import StepKind
from deployments.adapters.registry import get_deployment_adapter
from deployments.models import DeploymentTarget

from . import services as operation_services
from .models import (
    Operation,
    OperationType,
    StepExecution,
    StepExecutionStatus,
)


class StepRunnerError(operation_services.OperationServiceError):
    """Ошибка автоматического выполнения шага операции."""


# ==========================================================
# ПОЛУЧЕНИЕ ПРОВЕРКА ШАГА
# ==========================================================

def _get_step_context(step_execution_id):
    """
    Получает шаг, операцию, карточку и целевую установку.
    """

    try:
        step = (
            StepExecution.objects
            .select_related(
                "operation__change_ticket__deployment_target",
                "operation__backup_used",
            )
            .get(pk=step_execution_id)
        )
    except StepExecution.DoesNotExist:
        raise StepRunnerError(
            "Шаг операции не найден."
        )

    operation = step.operation
    ticket = operation.change_ticket

    if not ticket.deployment_target_id:
        raise StepRunnerError(
            "У карточки изменения не указана целевая установка."
        )

    target = ticket.deployment_target

    return operation, step, ticket, target


def _validate_automatic_step(operation, step):
    """
    Проверяет, что выбранный тип шага поддерживается
    автоматическим исполнителем.
    """

    if step.step_kind == StepKind.OTHER:
        raise StepRunnerError(
            "Этот шаг предназначен для ручного выполнения. "
            "Используйте обычное начало и завершение шага."
        )

    if operation.operation_type == OperationType.UPDATE:

        if step.step_kind not in {
            StepKind.BACKUP,
            StepKind.UPDATE,
            StepKind.VERIFICATION,
        }:
            raise StepRunnerError(
                "Для операции обновления указан "
                "неподдерживаемый тип шага."
            )

    elif operation.operation_type == OperationType.ROLLBACK:

        if step.step_kind not in {
            StepKind.UPDATE,
            StepKind.VERIFICATION,
        }:
            raise StepRunnerError(
                "Для операции отката указан "
                "неподдерживаемый тип шага."
            )

    else:
        raise StepRunnerError(
            "Неизвестный тип операции."
        )


# ==========================================================
# ОБРАБОТКА РЕЗУЛЬТАТОВ
# ==========================================================

def _serialize_result(data):
    """
    Сохраняет результат в JSON.

    Это позволяет показывать человеку понятное описание
    и впоследствии извлекать данные для следующих шагов.
    """

    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
    )


def _load_result(step):
    """
    Читает JSON-результат ранее выполненного шага.
    """

    try:
        data = json.loads(step.result)
    except (TypeError, ValueError):
        raise StepRunnerError(
            "Не удалось прочитать результат предыдущего шага."
        )

    if not isinstance(data, dict):
        raise StepRunnerError(
            "Результат предыдущего шага имеет неверный формат."
        )

    return data


def _get_rollback_expected_version(operation, verification_step):
    """
    Получает версию, восстановленную предыдущим шагом отката.
    """

    restore_step = (
        StepExecution.objects
        .filter(
            operation=operation,
            step_order__lt=verification_step.step_order,
            step_kind=StepKind.UPDATE,
            status=StepExecutionStatus.COMPLETED,
        )
        .order_by("-step_order")
        .first()
    )

    if restore_step is None:
        raise StepRunnerError(
            "Не найден успешно завершённый шаг восстановления."
        )

    result = _load_result(restore_step)

    if result.get("action") != "restore":
        raise StepRunnerError(
            "Предыдущий шаг не содержит результата восстановления."
        )

    version = result.get("version")

    if not isinstance(version, str) or not version.strip():
        raise StepRunnerError(
            "В результате восстановления отсутствует версия."
        )

    return version


# ==========================================================
# РЕЗЕРВНОЕ КОПИРОВАНИЕ
# ==========================================================

def _execute_backup(adapter, operation, ticket, actor):
    """
    Создаёт и проверяет резервную копию исходной версии ПО.
    """

    before = adapter.inspect()

    if not before.installed:
        raise StepRunnerError(
            "Нельзя создать резервную копию: "
            "в целевой установке нет установленной версии."
        )

    expected_version = str(
        ticket.source_version or ""
    ).strip()

    if not expected_version:
        raise StepRunnerError(
            "В карточке не указана исходная версия программного обеспечения."
        )

    if before.current_version != expected_version:
        raise StepRunnerError(
            "Фактическая версия на стенде не совпадает "
            "с исходной версией карточки. "
            f"Ожидается: {expected_version}; "
            f"обнаружена: {before.current_version}."
        )

    backup = adapter.create_backup(
        operation_id=operation.pk,
        actor=actor,
    )

    verified_backup = verify_backup(
        backup_id=backup.pk,
        actor=actor,
    )

    if not verified_backup.is_usable:
        reason = (
            verified_backup.verification_error
            or "Целостность резервной копии не подтверждена."
        )

        raise StepRunnerError(
            f"Резервная копия создана, но проверка не пройдена. "
            f"Идентификатор: {backup.pk}. {reason}"
        )

    return {
        "action": "backup",
        "summary": "Резервная копия создана и проверена.",
        "backup_id": str(verified_backup.pk),
        "version": before.current_version,
        "file_sha256": verified_backup.file_sha256,
        "size_bytes": verified_backup.size_bytes,
        "integrity_status": verified_backup.integrity_status,
    }


# ==========================================================
# УСТАНОВКА НОВОЙ ВЕРСИИ
# ==========================================================

def _execute_deployment(
    adapter,
    operation,
    ticket,
    target,
    actor,
):
    """
    Выполняет установку пакета обновления.

    Перед установкой:
    - проверяет завершённый шаг резервного копирования;
    - проверяет соответствие резервной копии операции;
    - повторно проверяет её целостность;
    - проверяет фактическую исходную версию.

    После установки:
    - проверяет результат адаптера;
    - выполняет health_check;
    - только после успешной проверки возвращает результат.
    """

    # --------------------------------------------------------
    # 1. Проверяем завершённый шаг резервного копирования.
    # --------------------------------------------------------

    backup_step = (
        StepExecution.objects
        .filter(
            operation=operation,
            step_order=1,
            step_kind=StepKind.BACKUP,
            status=StepExecutionStatus.COMPLETED,
        )
        .first()
    )

    if backup_step is None:
        raise StepRunnerError(
            "Нельзя выполнять обновление: "
            "первый шаг резервного копирования не завершён."
        )

    backup_data = _load_result(backup_step)

    if backup_data.get("action") != "backup":
        raise StepRunnerError(
            "Результат первого шага не подтверждает создание "
            "резервной копии."
        )

    # --------------------------------------------------------
    # 2. Проверяем версию, сохранённую в резервной копии.
    # --------------------------------------------------------

    expected_source_version = str(
        ticket.source_version or ""
    ).strip()

    if not expected_source_version:
        raise StepRunnerError(
            "В карточке не указана исходная версия ПО."
        )

    if backup_data.get("version") != expected_source_version:
        raise StepRunnerError(
            "Версия в результате резервного копирования "
            "не совпадает с исходной версией карточки."
        )

    # --------------------------------------------------------
    # 3. Проверяем принадлежность резервной копии операции.
    # --------------------------------------------------------

    backup = (
        Backup.objects
        .filter(operation=operation)
        .order_by("-created_at")
        .first()
    )

    if (
        backup is None
        or str(backup.pk) != str(backup_data.get("backup_id"))
    ):
        raise StepRunnerError(
            "Не найдена резервная копия, зарегистрированная "
            "первым шагом этой операции."
        )

    # --------------------------------------------------------
    # 4. Повторно проверяем физический файл резервной копии.
    # --------------------------------------------------------

    verified_backup = verify_backup(
        backup_id=backup.pk,
        actor=actor,
    )

    if not verified_backup.is_usable:
        reason = (
            verified_backup.verification_error
            or "Целостность резервной копии не подтверждена."
        )

        raise StepRunnerError(
            "Обновление запрещено: резервная копия "
            f"не прошла повторную проверку. {reason}"
        )

    # --------------------------------------------------------
    # 5. Проверяем фактическую исходную версию стенда.
    # --------------------------------------------------------

    before = adapter.inspect()

    if not before.installed:
        raise StepRunnerError(
            "Перед обновлением на целевой установке "
            "не обнаружена установленная версия ПО."
        )

    if before.current_version != expected_source_version:
        raise StepRunnerError(
            "Перед установкой обнаружено несоответствие исходной версии. "
            f"Ожидается: {expected_source_version}; "
            f"обнаружена: {before.current_version}."
        )

    # --------------------------------------------------------
    # 6. Устанавливаем пакет.
    # --------------------------------------------------------

    result = adapter.deploy(ticket)

    if result.target_id != str(target.pk):
        raise StepRunnerError(
            "Адаптер вернул идентификатор другой целевой установки."
        )

    expected_target_version = str(ticket.target_version)

    if (
        not result.version
        or str(result.version) != expected_target_version
        or not result.release_id
    ):
        raise StepRunnerError(
            "Результат установки не подтверждает ожидаемую "
            "целевую версию."
        )

    # --------------------------------------------------------
    # 7. Обязательно проверяем фактическую установленную версию.
    # --------------------------------------------------------

    health = adapter.health_check(
        expected_version=expected_target_version,
    )

    if not health.ok:
        raise StepRunnerError(
            "Установка выполнена, но проверка целевой установки "
            f"не пройдена. {health.message}"
        )

    if health.version != expected_target_version:
        raise StepRunnerError(
            "После установки обнаружена неожиданная версия. "
            f"Ожидалась: {expected_target_version}; "
            f"обнаружена: {health.version}."
        )

    return {
        "action": "deploy",
        "summary": result.message,
        "version": result.version,
        "release_id": result.release_id,
        "target_id": result.target_id,
        "source_version": before.current_version,
        "backup_id": str(verified_backup.pk),
        "backup_sha256": verified_backup.file_sha256,
        "health_check": health.message,
        "health_check_ok": health.ok,
    }


# ==========================================================
# ВОССТАНОВЛЕНИЕ РЕЗЕРВНОЙ КОПИИ
# ==========================================================

def _execute_restore(adapter, operation, actor, target):
    """
    Восстанавливает установку из выбранной резервной копии.
    """

    if not operation.backup_used_id:
        raise StepRunnerError(
            "Для операции отката не выбрана резервная копия."
        )

    result = adapter.restore(
        backup_id=operation.backup_used_id,
        actor=actor,
    )

    if result.target_id != str(target.pk):
        raise StepRunnerError(
            "Адаптер восстановил другую целевую установку."
        )

    if not result.version or not result.release_id:
        raise StepRunnerError(
            "Адаптер не вернул подтверждённые данные восстановленной версии."
        )

    return {
        "action": "restore",
        "summary": result.message,
        "version": result.version,
        "release_id": result.release_id,
        "target_id": result.target_id,
        "backup_id": str(operation.backup_used_id),
    }


# ==========================================================
# ПРОВЕРКА УСТАНОВЛЕННОЙ ВЕРСИИ
# ==========================================================

def _execute_health_check(
    adapter,
    operation,
    step,
    ticket,
):
    """
    Проверяет версию после обновления или восстановления.
    """

    if operation.operation_type == OperationType.UPDATE:
        expected_version = str(ticket.target_version)

    else:
        expected_version = _get_rollback_expected_version(
            operation,
            step,
        )

    health = adapter.health_check(
        expected_version=expected_version,
    )

    if not health.ok:
        raise StepRunnerError(
            f"Проверка целевой установки не пройдена. "
            f"{health.message}"
        )

    if health.version != expected_version:
        raise StepRunnerError(
            "Фактическая версия не совпадает с ожидаемой. "
            f"Ожидалась: {expected_version}; "
            f"обнаружена: {health.version}."
        )

    return {
        "action": "health_check",
        "summary": health.message,
        "version": health.version,
        "expected_version": expected_version,
        "ok": True,
    }


# ==========================================================
# ВЫБОР ФИЗИЧЕСКОГО ДЕЙСТВИЯ
# ==========================================================

def _perform_action(
    adapter,
    operation,
    step,
    ticket,
    target,
    actor,
):
    """
    Выбирает действие по типу операции и типу шага.
    """

    if (
        operation.operation_type == OperationType.UPDATE
        and step.step_kind == StepKind.BACKUP
    ):
        return _execute_backup(
            adapter,
            operation,
            ticket,
            actor,
        )

    if (
        operation.operation_type == OperationType.UPDATE
        and step.step_kind == StepKind.UPDATE
    ):
        return _execute_deployment(
            adapter,
            operation,
            ticket,
            target,
            actor,
        )

    if (
        operation.operation_type == OperationType.ROLLBACK
        and step.step_kind == StepKind.UPDATE
    ):
        return _execute_restore(
            adapter,
            operation,
            actor,
            target,
        )

    if step.step_kind == StepKind.VERIFICATION:
        return _execute_health_check(
            adapter,
            operation,
            step,
            ticket,
        )

    raise StepRunnerError(
        "Для этого шага не определено автоматическое действие."
    )


# ==========================================================
# ОБНОВЛЕНИЕ СВЕДЕНИЙ О ЦЕЛЕВОЙ УСТАНОВКЕ
# ==========================================================

def _update_target_version(target_id, version):
    """
    Сохраняет последнюю подтверждённую версию установки.

    Глобальную версию программного обеспечения не изменяет.
    """

    if not version:
        return

    target = (
        DeploymentTarget.objects
        .select_for_update()
        .get(pk=target_id)
    )

    target.last_known_version = str(version)
    target.last_inspected_at = timezone.now()

    target.save(
        update_fields=[
            "last_known_version",
            "last_inspected_at",
            "updated_at",
        ]
    )


def _refresh_target_after_failure(adapter, target_id):
    """
    После ошибки физического действия пытается выяснить,
    какая версия реально осталась активной.

    Это важно, поскольку физическое изменение файловой системы
    не отменяется откатом транзакции PostgreSQL.
    """

    try:
        inspection = adapter.inspect()
    except Exception:
        return None

    try:
        with transaction.atomic():
            _update_target_version(
                target_id,
                inspection.current_version or "",
            )
    except Exception:
        # Ошибка обновления справочной информации не должна
        # скрывать первоначальную ошибку адаптера.
        return inspection

    return inspection


# ==========================================================
# ПРЕОБРАЗОВАНИЕ ОШИБКИ В ПОНЯТНОЕ СООБЩЕНИЕ
# ==========================================================

def _format_error(error):
    """
    Получает текст ошибки без технического представления
    объекта исключения Django.
    """

    if isinstance(error, ValidationError):
        messages = getattr(error, "messages", None)

        if messages:
            message = "; ".join(str(item) for item in messages)
        else:
            message = str(error)

    else:
        message = str(error).strip()

    if not message:
        message = (
            f"Неизвестная ошибка ({type(error).__name__})."
        )

    return message[:4000]


# ==========================================================
# ЗАВЕРШЕНИЕ ШАГА С РЕЗУЛЬТАТОМ
# ==========================================================

def _complete_with_result(
    step_execution_id,
    operation_id,
    target_id,
    result_data,
    actor,
):
    """
    Сохраняет версию целевой установки и завершает шаг
    в одной транзакции PostgreSQL.

    Само физическое действие к этому моменту уже завершено.
    """

    result_text = _serialize_result(result_data)

    confirmed_version = result_data.get("version")

    with transaction.atomic():

        # Блокируем операцию перед фиксацией результата.
        Operation.objects.select_for_update().get(
            pk=operation_id
        )

        # После deploy, restore и health_check версия
        # подтверждена адаптером.
        if confirmed_version:
            _update_target_version(
                target_id,
                confirmed_version,
            )

        # Существующая функция проверяет права, завершает шаг,
        # разблокирует следующий и при необходимости завершает операцию.
        operation_services.complete_step(
            step_execution_id=step_execution_id,
            actor=actor,
            result=result_text,
        )

    return StepExecution.objects.get(
        pk=step_execution_id
    )


# ==========================================================
# ОСНОВНАЯ ФУНКЦИЯ ИСПОЛНЕНИЯ
# ==========================================================

def execute_step(step_execution_id, actor):
    """
    Полностью выполняет один автоматический шаг.

    Последовательность:

    1. Проверяет пользователя и принадлежность карточки.
    2. Переводит доступный шаг в IN_PROGRESS.
    3. Получает адаптер целевой установки.
    4. Выполняет физическое действие.
    5. Сохраняет результат и актуальную версию.
    6. Завершает шаг через существующий сервис.

    При ошибке:
    - сохраняет реальную ошибку;
    - по возможности выясняет фактическую установленную версию;
    - вызывает fail_step().
    """

    # Сначала проверяем роль пользователя.
    operation_services._require_executor(actor)

    operation, step, ticket, target = _get_step_context(
        step_execution_id
    )

    # Проверяем владельца карточки.
    operation_services._require_operation_owner(
        actor,
        operation,
    )

    # Не запускаем шаги ручного типа.
    _validate_automatic_step(
        operation,
        step,
    )

    # Фиксируем начало шага отдельной транзакцией.
    # Блокировка БД не удерживается во время работы адаптера.
    started_step = operation_services.start_step(
        step_execution_id=step_execution_id,
        actor=actor,
    )

    adapter = None

    try:
        # Целевая установка должна оставаться активной.
        if not target.can_execute:
            raise StepRunnerError(
                "Целевая установка неактивна или недостаточно настроена."
            )

        adapter = get_deployment_adapter(target)

        # Реальное действие выполняется вне транзакции БД.
        result_data = _perform_action(
            adapter=adapter,
            operation=operation,
            step=started_step,
            ticket=ticket,
            target=target,
            actor=actor,
        )

    except Exception as error:
        error_text = _format_error(error)

        # При установке, восстановлении или проверке выясняем,
        # какая версия действительно осталась активной.
        inspection = None

        if (
            adapter is not None
            and step.step_kind in {
                StepKind.UPDATE,
                StepKind.VERIFICATION,
            }
        ):
            inspection = _refresh_target_after_failure(
                adapter,
                target.pk,
            )

        if inspection is not None:
            actual_version = (
                inspection.current_version
                or "установленная версия отсутствует"
            )

            error_text += (
                "\nФактическое состояние целевой установки: "
                f"{actual_version}."
            )

        # Существующий сервис зафиксирует FAILED
        # для шага и всей операции.
        operation_services.fail_step(
            step_execution_id=step_execution_id,
            actor=actor,
            error_message=error_text,
        )

        return StepExecution.objects.get(
            pk=step_execution_id
        )

    # Действие адаптера завершилось успешно.
    # Сохраняем его результат и завершаем шаг.
    return _complete_with_result(
        step_execution_id=step_execution_id,
        operation_id=operation.pk,
        target_id=target.pk,
        result_data=result_data,
        actor=actor,
    )