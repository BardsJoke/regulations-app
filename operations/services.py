import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment
from audit.services import AuditAction, record_event
from catalogs.models import CardStatus, CardStatusCode, RollbackReason
from changes.models import (
    ChangeTicket,
    CompatibilityStatus,
    HashStatus,
    PlanStep,
    ResourcesCheckStatus,
    StepKind,
)
from deployments.models import DeploymentTarget

from .models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
    StepTemplate,
)


class OperationServiceError(ValidationError):
    """Ошибка бизнес-правил при выполнении операции."""


# ============================================================
# ПРОВЕРКА ПРАВ И ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def _require_executor(actor):
    """
    Проверяет, что пользователь авторизован, активен
    и имеет роль исполнителя.
    """

    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied(
            "Для выполнения операции необходимо войти в систему."
        )

    # В модели RoleAssignment отсутствует поле is_active.
    has_executor_role = RoleAssignment.objects.filter(
        user=actor,
        role=ModuleRole.EXECUTOR,
    ).exists()

    if not has_executor_role:
        raise PermissionDenied(
            "Недостаточно прав: требуется роль исполнителя."
        )


def _calculate_package_sha256(package_file):
    """
    Вычисляет SHA-256 загруженного пакета обновления.
    """

    if not package_file:
        raise OperationServiceError(
            "Не загружен пакет обновления."
        )

    digest = hashlib.sha256()

    package_file.open("rb")

    try:
        for chunk in package_file.chunks():
            digest.update(chunk)
    finally:
        package_file.close()

    return digest.hexdigest()


def _lock_and_validate_deployment_target(ticket):
    """
    Блокирует целевую установку и проверяет возможность
    выполнения операции на ней.

    is_configured проверяет настройки в базе данных.
    Реальное соединение с установкой будет проверяться
    отдельным адаптером развертывания.
    """

    if not ticket.deployment_target_id:
        raise OperationServiceError(
            "Для карточки не указана целевая установка."
        )

    try:
        target = (
            DeploymentTarget.objects
            .select_for_update()
            .select_related("software")
            .get(pk=ticket.deployment_target_id)
        )
    except DeploymentTarget.DoesNotExist:
        raise OperationServiceError(
            "Целевая установка не найдена."
        )

    if target.software_id != ticket.software_id:
        raise OperationServiceError(
            "Целевая установка относится к другому программному обеспечению."
        )

    if not target.is_configured:
        raise OperationServiceError(
            "Целевая установка не настроена."
        )

    if not target.is_active:
        raise OperationServiceError(
            "Целевая установка неактивна."
        )

    if not target.software.is_active:
        raise OperationServiceError(
            "Программное обеспечение целевой установки неактивно."
        )

    return target


def _lock_operation_and_step(step_execution_id):
    """
    Блокирует операцию и соответствующий шаг.

    Идентификатор операции определяется через StepExecution.
    """

    try:
        step_reference = (
            StepExecution.objects
            .only("operation_id")
            .get(pk=step_execution_id)
        )
    except StepExecution.DoesNotExist:
        raise OperationServiceError(
            "Шаг операции не найден."
        )

    try:
        operation = (
            Operation.objects
            .select_for_update()
            .select_related("change_ticket")
            .get(pk=step_reference.operation_id)
        )
    except Operation.DoesNotExist:
        raise OperationServiceError(
            "Операция не найдена."
        )

    try:
        step = (
            StepExecution.objects
            .select_for_update()
            .get(
                pk=step_execution_id,
                operation=operation,
            )
        )
    except StepExecution.DoesNotExist:
        raise OperationServiceError(
            "Шаг не найден в указанной операции."
        )

    return operation, step


def _require_running_operation(operation):
    """
    Проверяет, что операция находится в состоянии RUNNING.
    """

    if operation.status != OperationStatus.RUNNING:
        raise OperationServiceError(
            "Нельзя выполнять действия: операция не находится в работе."
        )


def _get_card_status(code):
    """
    Получает статус карточки по его коду.
    """

    try:
        return CardStatus.objects.get(code=code)
    except CardStatus.DoesNotExist:
        raise OperationServiceError(
            f"В справочнике не найден статус карточки: {code}."
        )


def _set_ticket_status(ticket, code):
    """
    Изменяет статус карточки изменения.
    """

    ticket.status = _get_card_status(code)
    ticket.save(update_fields=["status"])


def _set_operation_error(operation, error_text):
    """
    Записывает текст ошибки операции.

    Учитывает возможные названия поля error_message и error.
    """

    concrete_field_names = {
        field.name
        for field in Operation._meta.concrete_fields
    }

    if "error_message" in concrete_field_names:
        field_name = "error_message"
    elif "error" in concrete_field_names:
        field_name = "error"
    else:
        raise OperationServiceError(
            "В модели Operation не найдено поле для хранения ошибки."
        )

    setattr(operation, field_name, error_text)

    return field_name


# ============================================================
# АУДИТ
# ============================================================

def _record_operation_event(actor, action, operation, details=None):
    """
    Записывает событие, связанное с операцией.
    """

    record_event(
        actor=actor,
        action=action,
        object_type=Operation._meta.label_lower,
        object_id=operation.pk,
        details=details or {},
    )


def _record_step_event(actor, action, step, details=None):
    """
    Записывает событие, связанное с конкретным шагом.
    """

    event_details = dict(details or {})
    event_details["operation_id"] = str(step.operation_id)

    record_event(
        actor=actor,
        action=action,
        object_type=StepExecution._meta.label_lower,
        object_id=step.pk,
        details=event_details,
    )


# ============================================================
# ВЫПОЛНЕНИЕ ОТДЕЛЬНЫХ ШАГОВ
# ============================================================

def start_step(step_execution_id, actor):
    """
    Начинает выполнение доступного шага операции.

    Проверяет:
    - права исполнителя;
    - состояние операции;
    - доступность шага;
    - завершение предыдущих шагов;
    - отсутствие другого выполняемого шага.
    """

    _require_executor(actor)

    with transaction.atomic():
        operation, step = _lock_operation_and_step(
            step_execution_id
        )

        _require_running_operation(operation)

        if step.status != StepExecutionStatus.AVAILABLE:
            raise OperationServiceError(
                "Этот шаг пока недоступен для выполнения."
            )

        another_running_step = (
            StepExecution.objects
            .filter(
                operation=operation,
                status=StepExecutionStatus.IN_PROGRESS,
            )
            .exclude(pk=step.pk)
            .exists()
        )

        if another_running_step:
            raise OperationServiceError(
                "Нельзя начать новый шаг: другой шаг этой операции "
                "ещё выполняется."
            )

        previous_steps = StepExecution.objects.filter(
            operation=operation,
            step_order__lt=step.step_order,
        )

        if previous_steps.exclude(
            status=StepExecutionStatus.COMPLETED
        ).exists():
            raise OperationServiceError(
                "Нельзя начать шаг: предыдущие шаги ещё не завершены."
            )

        step.status = StepExecutionStatus.IN_PROGRESS
        step.started_by = actor
        step.started_at = timezone.now()

        step.save(
            update_fields=[
                "status",
                "started_by",
                "started_at",
            ]
        )

        _record_step_event(
            actor=actor,
            action=AuditAction.STEP_STARTED,
            step=step,
            details={
                "step_order": step.step_order,
                "step_name": step.name,
            },
        )

    return step


def complete_step(step_execution_id, actor, result=""):
    """
    Завершает выполняемый шаг.

    После завершения:
    - следующий шаг становится доступным;
    - при завершении всех шагов операция завершается;
    - карточка получает итоговый статус;
    - событие фиксируется в аудите.
    """

    _require_executor(actor)

    with transaction.atomic():
        operation, step = _lock_operation_and_step(
            step_execution_id
        )

        _require_running_operation(operation)

        if step.status != StepExecutionStatus.IN_PROGRESS:
            raise OperationServiceError(
                "Завершить можно только шаг, который находится в работе."
            )

        step.status = StepExecutionStatus.COMPLETED
        step.finished_by = actor
        step.finished_at = timezone.now()
        step.result = result or ""

        step.save(
            update_fields=[
                "status",
                "finished_by",
                "finished_at",
                "result",
            ]
        )

        _record_step_event(
            actor=actor,
            action=AuditAction.STEP_COMPLETED,
            step=step,
            details={
                "step_order": step.step_order,
                "step_name": step.name,
                "result": result or "",
            },
        )

        # Разблокируем следующий шаг.
        next_step = (
            StepExecution.objects
            .select_for_update()
            .filter(
                operation=operation,
                step_order__gt=step.step_order,
            )
            .order_by("step_order")
            .first()
        )

        if (
            next_step
            and next_step.status == StepExecutionStatus.LOCKED
        ):
            next_step.status = StepExecutionStatus.AVAILABLE
            next_step.save(
                update_fields=["status"]
            )

        # Проверяем, остались ли незавершённые шаги.
        unfinished_steps_exist = (
            StepExecution.objects
            .filter(operation=operation)
            .exclude(status=StepExecutionStatus.COMPLETED)
            .exists()
        )

        if not unfinished_steps_exist:
            operation.status = OperationStatus.COMPLETED
            operation.finished_at = timezone.now()

            # В модели Operation нет поля result.
            # Результаты отдельных шагов хранятся в StepExecution.
            operation.save(
                update_fields=[
                    "status",
                    "finished_at",
                ]
            )

            if operation.operation_type == OperationType.ROLLBACK:
                ticket_status = CardStatusCode.ROLLED_BACK
            else:
                ticket_status = CardStatusCode.COMPLETED

            _set_ticket_status(
                operation.change_ticket,
                ticket_status,
            )

            _record_operation_event(
                actor=actor,
                action=AuditAction.OPERATION_COMPLETED,
                operation=operation,
                details={
                    "ticket_id": str(operation.change_ticket_id),
                    "operation_type": operation.operation_type,
                },
            )

    return step


def fail_step(step_execution_id, actor, error_message):
    """
    Завершает шаг с ошибкой и переводит операцию в FAILED.

    Ошибка шага сохраняется в поле StepExecution.error_message.
    Карточка остаётся в работе для последующей повторной попытки
    или выполнения отката.
    """

    _require_executor(actor)

    error_text = str(error_message or "").strip()

    if not error_text:
        raise OperationServiceError(
            "Для завершения шага с ошибкой необходимо указать её причину."
        )

    with transaction.atomic():
        operation, step = _lock_operation_and_step(
            step_execution_id
        )

        _require_running_operation(operation)

        if step.status != StepExecutionStatus.IN_PROGRESS:
            raise OperationServiceError(
                "Завершить с ошибкой можно только выполняемый шаг."
            )

        step.status = StepExecutionStatus.FAILED
        step.finished_by = actor
        step.finished_at = timezone.now()
        step.error_message = error_text

        step.save(
            update_fields=[
                "status",
                "finished_by",
                "finished_at",
                "error_message",
            ]
        )

        operation.status = OperationStatus.FAILED
        operation.finished_at = timezone.now()

        error_field = _set_operation_error(
            operation,
            error_text,
        )

        operation.save(
            update_fields=[
                "status",
                "finished_at",
                error_field,
            ]
        )

        _record_step_event(
            actor=actor,
            action=AuditAction.STEP_FAILED,
            step=step,
            details={
                "step_order": step.step_order,
                "step_name": step.name,
                "error": error_text,
            },
        )

        _record_operation_event(
            actor=actor,
            action=AuditAction.OPERATION_FAILED,
            operation=operation,
            details={
                "ticket_id": str(operation.change_ticket_id),
                "failed_step_id": str(step.pk),
                "error": error_text,
            },
        )

    return step


# ============================================================
# ЗАПУСК ОБНОВЛЕНИЯ
# ============================================================

def start_update_operation(ticket_id, actor):
    """
    Запускает операцию обновления программного обеспечения.

    Перед запуском проверяются:
    - права исполнителя;
    - принадлежность карточки пользователю;
    - целевая установка;
    - согласование;
    - временное окно;
    - совместимость;
    - доступность ресурсов;
    - последовательность плана;
    - наличие пакета обновления;
    - контрольная сумма SHA-256;
    - отсутствие другой выполняющейся операции.
    """

    _require_executor(actor)

    hash_mismatch = False
    operation = None

    with transaction.atomic():
        try:
            ticket = (
                ChangeTicket.objects
                .select_for_update()
                .select_related(
                    "software",
                    "status",
                    # deployment_target намеренно не включён сюда:
                    # это nullable FK, а целевая установка отдельно
                    # блокируется в _lock_and_validate_deployment_target().
                )
                .get(pk=ticket_id)
            )
        except ChangeTicket.DoesNotExist:
            raise OperationServiceError(
                "Карточка изменения не найдена."
            )

        if ticket.created_by_id != actor.id:
            raise PermissionDenied(
                "Исполнитель может запускать обновление только "
                "для собственной карточки изменения."
            )

        # Проверяем и блокируем целевую установку.
        _lock_and_validate_deployment_target(ticket)

        # Проверяем статус карточки.
        if ticket.status.code == CardStatusCode.APPROVED:
            pass

        elif ticket.status.code == CardStatusCode.IN_PROGRESS:
            last_update = (
                Operation.objects
                .filter(
                    change_ticket=ticket,
                    operation_type=OperationType.UPDATE,
                )
                .order_by("-attempt_number")
                .first()
            )

            if (
                last_update is None
                or last_update.status != OperationStatus.FAILED
            ):
                raise OperationServiceError(
                    "Повторный запуск обновления допустим только "
                    "после неудачной предыдущей попытки."
                )

        else:
            raise OperationServiceError(
                "Обновление можно запускать только для согласованной "
                "карточки или после неудачной попытки обновления."
            )

        if not ticket.approved_by_id:
            raise OperationServiceError(
                "У карточки отсутствует подтверждение согласования."
            )

        # Проверяем плановое окно.
        now = timezone.now()

        if ticket.window_start_at and now < ticket.window_start_at:
            raise OperationServiceError(
                "Плановое окно обновления ещё не началось."
            )

        if ticket.window_end_at and now > ticket.window_end_at:
            raise OperationServiceError(
                "Плановое окно обновления уже закончилось."
            )

        # Проверяем совместимость.
        if ticket.compatibility_status != CompatibilityStatus.COMPATIBLE:
            raise OperationServiceError(
                "Не подтверждена совместимость обновления."
            )

        # Проверяем доступность ресурсов.
        if ticket.resources_check_status != ResourcesCheckStatus.SUFFICIENT:
            raise OperationServiceError(
                "Не подтверждена достаточность ресурсов."
            )

        # Получаем плановые шаги.
        plan_steps = list(
            PlanStep.objects
            .filter(ticket=ticket)
            .order_by("step_order")
        )

        if not plan_steps:
            raise OperationServiceError(
                "У карточки отсутствует план выполнения."
            )

        expected_order = list(
            range(1, len(plan_steps) + 1)
        )

        actual_order = [
            step.step_order
            for step in plan_steps
        ]

        if actual_order != expected_order:
            raise OperationServiceError(
                "Порядок плановых шагов должен быть последовательным, "
                "начиная с единицы."
            )

        if plan_steps[0].step_kind != StepKind.BACKUP:
            raise OperationServiceError(
                "Первым шагом плана должно быть резервное копирование."
            )

        # Проверяем наличие пакета.
        if not ticket.package_file:
            raise OperationServiceError(
                "Для карточки не загружен пакет обновления."
            )

        if not ticket.expected_sha256:
            raise OperationServiceError(
                "Для пакета обновления не указана ожидаемая SHA-256."
            )

        # Вычисляем фактическую контрольную сумму.
        actual_sha256 = _calculate_package_sha256(
            ticket.package_file
        )

        hash_mismatch = (
            actual_sha256.lower()
            != ticket.expected_sha256.lower()
        )

        ticket.actual_sha256 = actual_sha256
        ticket.hash_status = (
            HashStatus.MISMATCH
            if hash_mismatch
            else HashStatus.MATCH
        )
        ticket.hash_checked_at = timezone.now()

        ticket.save(
            update_fields=[
                "actual_sha256",
                "hash_status",
                "hash_checked_at",
                "updated_at",
            ]
        )

        if not hash_mismatch:
            # Проверяем, что другая операция не выполняется.
            if Operation.objects.filter(
                change_ticket=ticket,
                status=OperationStatus.RUNNING,
            ).exists():
                raise OperationServiceError(
                    "Для этой карточки уже выполняется другая операция."
                )

            max_sequence = (
                Operation.objects
                .filter(change_ticket=ticket)
                .aggregate(value=Max("sequence_number"))["value"]
                or 0
            )

            max_attempt = (
                Operation.objects
                .filter(
                    change_ticket=ticket,
                    operation_type=OperationType.UPDATE,
                )
                .aggregate(value=Max("attempt_number"))["value"]
                or 0
            )

            # Создаём операцию обновления.
            operation = Operation.objects.create(
                change_ticket=ticket,
                operation_type=OperationType.UPDATE,
                sequence_number=max_sequence + 1,
                attempt_number=max_attempt + 1,
                initiated_by=actor,
                status=OperationStatus.RUNNING,
            )

            # Создаём снимки плановых шагов.
            for index, plan_step in enumerate(plan_steps, start=1):
                StepExecution.objects.create(
                    operation=operation,
                    plan_step=plan_step,
                    step_order=plan_step.step_order,
                    step_kind=plan_step.step_kind,
                    name=plan_step.name,
                    description=plan_step.description,
                    planned_duration_min=plan_step.planned_duration_min,
                    status=(
                        StepExecutionStatus.AVAILABLE
                        if index == 1
                        else StepExecutionStatus.LOCKED
                    ),
                )

            # Переводим согласованную карточку в работу.
            if ticket.status.code == CardStatusCode.APPROVED:
                _set_ticket_status(
                    ticket,
                    CardStatusCode.IN_PROGRESS,
                )

            _record_operation_event(
                actor=actor,
                action=AuditAction.UPDATE_STARTED,
                operation=operation,
                details={
                    "ticket_id": str(ticket.id),
                    "software_id": str(ticket.software_id),
                    "deployment_target_id": str(ticket.deployment_target_id),
                    "attempt_number": operation.attempt_number,
                },
            )

    # Ошибку поднимаем за пределами транзакции,
    # чтобы результат проверки SHA-256 сохранился в карточке.
    if hash_mismatch:
        raise OperationServiceError(
            "Контрольная сумма пакета обновления не совпадает "
            "с ожидаемой SHA-256. Запуск операции отменён."
        )

    return operation


# ============================================================
# ЗАПУСК ОТКАТА
# ============================================================

def start_rollback_operation(
    parent_operation_id,
    backup_id,
    rollback_reason_id,
    actor,
):
    """
    Запускает операцию отката к резервной копии.

    Проверяются:
    - права исполнителя;
    - исходная операция обновления;
    - принадлежность карточки;
    - целевая установка;
    - статус карточки;
    - последняя попытка обновления;
    - отсутствие другой выполняющейся операции;
    - причина отката;
    - принадлежность и целостность резервной копии;
    - наличие шаблонов шагов отката.
    """

    _require_executor(actor)

    # Локальные импорты помогают избежать возможных циклических импортов.
    from backups.models import Backup
    from backups.services import verify_backup

    backup_verification_failed = False
    rollback_operation = None

    with transaction.atomic():
        try:
            parent_operation = (
                Operation.objects
                .select_for_update()
                .select_related("change_ticket")
                .get(pk=parent_operation_id)
            )
        except Operation.DoesNotExist:
            raise OperationServiceError(
                "Исходная операция обновления не найдена."
            )

        if parent_operation.operation_type != OperationType.UPDATE:
            raise OperationServiceError(
                "Откат можно выполнить только для операции обновления."
            )

        if parent_operation.status not in (
            OperationStatus.FAILED,
            OperationStatus.COMPLETED,
        ):
            raise OperationServiceError(
                "Откат возможен только после завершения или ошибки обновления."
            )

        ticket = (
            ChangeTicket.objects
            .select_for_update()
            .select_related(
                "software",
                "status",
                # deployment_target намеренно не включён сюда:
                # это nullable FK, а целевая установка отдельно
                # блокируется в _lock_and_validate_deployment_target().
            )
            .get(pk=parent_operation.change_ticket_id)
        )

        if ticket.created_by_id != actor.id:
            raise PermissionDenied(
                "Исполнитель может выполнять откат только "
                "для собственной карточки изменения."
            )

        # Проверяем целевую установку перед откатом.
        _lock_and_validate_deployment_target(ticket)

        if ticket.status.code not in (
            CardStatusCode.IN_PROGRESS,
            CardStatusCode.COMPLETED,
        ):
            raise OperationServiceError(
                "Текущий статус карточки не допускает выполнение отката."
            )

        # Откат допускается только для последней попытки обновления.
        latest_update = (
            Operation.objects
            .filter(
                change_ticket=ticket,
                operation_type=OperationType.UPDATE,
            )
            .order_by("-attempt_number")
            .first()
        )

        if (
            latest_update is None
            or latest_update.pk != parent_operation.pk
        ):
            raise OperationServiceError(
                "Откат можно выполнить только для последней попытки обновления."
            )

        # Проверяем, не был ли уже успешно выполнен откат.
        if Operation.objects.filter(
            change_ticket=ticket,
            operation_type=OperationType.ROLLBACK,
            status=OperationStatus.COMPLETED,
        ).exists():
            raise OperationServiceError(
                "Для этой карточки уже существует успешно завершённый откат."
            )

        # Одновременно может выполняться только одна операция.
        if Operation.objects.filter(
            change_ticket=ticket,
            status=OperationStatus.RUNNING,
        ).exists():
            raise OperationServiceError(
                "Для этой карточки уже выполняется другая операция."
            )

        # Проверяем причину отката.
        try:
            rollback_reason = (
                RollbackReason.objects
                .select_for_update()
                .get(
                    pk=rollback_reason_id,
                    is_active=True,
                )
            )
        except RollbackReason.DoesNotExist:
            raise OperationServiceError(
                "Причина отката не найдена или неактивна."
            )

        # Получаем резервную копию.
        try:
            backup = (
                Backup.objects
                .select_for_update()
                .get(pk=backup_id)
            )
        except Backup.DoesNotExist:
            raise OperationServiceError(
                "Резервная копия не найдена."
            )

        if backup.operation_id != parent_operation.id:
            raise OperationServiceError(
                "Выбранная резервная копия не относится "
                "к исходной операции обновления."
            )

        # Получаем активные шаблоны шагов отката.
        templates = list(
            StepTemplate.objects
            .filter(
                operation_type=OperationType.ROLLBACK,
                is_active=True,
            )
            .order_by("sort_order")
        )

        if not templates:
            raise OperationServiceError(
                "Не настроены шаблоны шагов для операции отката."
            )

        # Повторно проверяем реальное содержимое файла.
        # verify_backup сохраняет результат проверки в базе.
        verified_backup = verify_backup(
            backup_id=backup.id,
            actor=actor,
        )

        if not verified_backup.is_usable:
            backup_verification_failed = True

        if not backup_verification_failed:
            max_sequence = (
                Operation.objects
                .filter(change_ticket=ticket)
                .aggregate(value=Max("sequence_number"))["value"]
                or 0
            )

            max_attempt = (
                Operation.objects
                .filter(
                    change_ticket=ticket,
                    operation_type=OperationType.ROLLBACK,
                )
                .aggregate(value=Max("attempt_number"))["value"]
                or 0
            )

            # Создаём операцию отката.
            rollback_operation = Operation.objects.create(
                change_ticket=ticket,
                operation_type=OperationType.ROLLBACK,
                sequence_number=max_sequence + 1,
                attempt_number=max_attempt + 1,
                parent_operation=parent_operation,
                backup_used=verified_backup,
                rollback_reason=rollback_reason,
                initiated_by=actor,
                status=OperationStatus.RUNNING,
            )

            # Создаём снимки шаблонов шагов отката.
            for index, template in enumerate(templates, start=1):
                StepExecution.objects.create(
                    operation=rollback_operation,
                    plan_step=None,
                    step_order=index,
                    step_kind=template.step_kind,
                    name=template.name,
                    description=template.description,
                    planned_duration_min=template.default_duration_min,
                    status=(
                        StepExecutionStatus.AVAILABLE
                        if index == 1
                        else StepExecutionStatus.LOCKED
                    ),
                )

            # Возвращаем завершённую карточку в работу.
            if ticket.status.code == CardStatusCode.COMPLETED:
                _set_ticket_status(
                    ticket,
                    CardStatusCode.IN_PROGRESS,
                )

            _record_operation_event(
                actor=actor,
                action=AuditAction.ROLLBACK_STARTED,
                operation=rollback_operation,
                details={
                    "ticket_id": str(ticket.id),
                    "parent_operation_id": str(parent_operation.id),
                    "backup_id": str(verified_backup.id),
                    "rollback_reason_id": str(rollback_reason.id),
                    "attempt_number": rollback_operation.attempt_number,
                },
            )

    # Поднимаем ошибку за пределами транзакции,
    # чтобы результат проверки резервной копии сохранился.
    if backup_verification_failed:
        raise OperationServiceError(
            "Проверка резервной копии завершилась неудачно. "
            "Запуск отката отменён."
        )

    return rollback_operation
