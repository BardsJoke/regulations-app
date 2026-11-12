from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment
from catalogs.models import CardStatus, CardStatusCode,  RollbackReason

from .models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
    StepTemplate,
)

import hashlib

from django.db.models import Max

from changes.models import (
    ChangeTicket,
    CompatibilityStatus,
    HashStatus,
    ResourcesCheckStatus,
    PlanStep,
    StepKind,
)


class OperationServiceError(ValidationError):
    """Ошибка при выполнении бизнес-операции."""

def _calculate_package_sha256(package_file):
    """
    Вычисляет SHA-256 непосредственно по содержимому файла.

    Файл читается частями, поэтому не нужно загружать
    весь пакет в оперативную память.
    """

    digest = hashlib.sha256()

    with package_file.open("rb") as package:
        for chunk in iter(
            lambda: package.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()

def _require_executor(actor):
    """
    Проверяет, что пользователь активен
    и имеет роль исполнителя.
    """

    if (
        not actor
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
    ):
        raise PermissionDenied(
            "Для выполнения действия необходима "
            "активная учётная запись."
        )

    has_role = RoleAssignment.objects.filter(
        user_id=actor.pk,
        role=ModuleRole.EXECUTOR,
    ).exists()

    if not has_role:
        raise PermissionDenied(
            "Только исполнитель может выполнять технические шаги."
        )


def _lock_operation_and_step(step_execution_id):
    """
    Блокирует операцию и шаг на время транзакции.
    Это защищает от параллельного выполнения действий.
    """

    try:
        step_reference = StepExecution.objects.only(
            "operation_id"
        ).get(pk=step_execution_id)
    except StepExecution.DoesNotExist:
        raise OperationServiceError(
            "Шаг выполнения не найден."
        )

    try:
        operation = (
            Operation.objects
            .select_for_update()
            .get(pk=step_reference.operation_id)
        )

        step = (
            StepExecution.objects
            .select_for_update()
            .get(
                pk=step_execution_id,
                operation=operation,
            )
        )

    except Operation.DoesNotExist:
        raise OperationServiceError(
            "Операция не найдена."
        )

    return operation, step


def _require_running_operation(operation):
    if operation.status != OperationStatus.RUNNING:
        raise OperationServiceError(
            "Нельзя изменять шаги завершённой или остановленной операции."
        )

@transaction.atomic
def start_step(*, step_execution_id, actor):
    """
    Начинает выполнение доступного шага.

    Предыдущий шаг должен быть завершён.
    Одновременно может выполняться только один шаг операции.
    """

    _require_executor(actor)

    operation, step = _lock_operation_and_step(
        step_execution_id
    )

    _require_running_operation(operation)

    if step.status != StepExecutionStatus.AVAILABLE:
        raise OperationServiceError(
            "Этот шаг сейчас недоступен для выполнения."
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
            "Сначала необходимо завершить текущий шаг."
        )

    if step.step_order > 1:
        previous_step = (
            StepExecution.objects
            .filter(
                operation=operation,
                step_order=step.step_order - 1,
            )
            .first()
        )

        if (
            previous_step is None
            or previous_step.status != StepExecutionStatus.COMPLETED
        ):
            raise OperationServiceError(
                "Нельзя начать шаг, пока предыдущий "
                "не завершён успешно."
            )

    step.status = StepExecutionStatus.IN_PROGRESS
    step.started_by = actor
    step.started_at = timezone.now()

    step.full_clean()

    step.save(
        update_fields=[
            "status",
            "started_by",
            "started_at",
            "updated_at",
        ]
    )

    return step


@transaction.atomic
def complete_step(
    *,
    step_execution_id,
    actor,
    result="",
):
    """
    Завершает текущий шаг.

    Если следующий шаг существует, он становится доступным.
    Если все шаги завершены, завершается операция и обновляется
    статус карточки изменения.
    """

    _require_executor(actor)

    operation, step = _lock_operation_and_step(
        step_execution_id
    )

    _require_running_operation(operation)

    if step.status != StepExecutionStatus.IN_PROGRESS:
        raise OperationServiceError(
            "Можно завершить только выполняющийся шаг."
        )

    now = timezone.now()

    step.status = StepExecutionStatus.COMPLETED
    step.finished_by = actor
    step.finished_at = now
    step.result = result

    step.full_clean()

    step.save(
        update_fields=[
            "status",
            "finished_by",
            "finished_at",
            "result",
            "updated_at",
        ]
    )

    next_step = (
        StepExecution.objects
        .select_for_update()
        .filter(
            operation=operation,
            step_order=step.step_order + 1,
        )
        .first()
    )

    if next_step:
        if next_step.status != StepExecutionStatus.LOCKED:
            raise OperationServiceError(
                "Обнаружено некорректное состояние следующего шага."
            )

        next_step.status = StepExecutionStatus.AVAILABLE

        next_step.save(
            update_fields=["status", "updated_at"]
        )

    else:
        unfinished_steps = (
            StepExecution.objects
            .filter(operation=operation)
            .exclude(status=StepExecutionStatus.COMPLETED)
            .exists()
        )

        if unfinished_steps:
            raise OperationServiceError(
                "Не все шаги операции завершены."
            )

        operation.status = OperationStatus.COMPLETED
        operation.finished_at = now

        if result:
            operation.result_summary = result

        operation.full_clean()

        operation.save(
            update_fields=[
                "status",
                "finished_at",
                "result_summary",
                "updated_at",
            ]
        )

        # После успешного завершения операции обновляем карточку.
        ticket_status_code = (
            CardStatusCode.COMPLETED
            if operation.operation_type == OperationType.UPDATE
            else CardStatusCode.ROLLED_BACK
        )

        ticket_status = CardStatus.objects.get(
            code=ticket_status_code
        )

        ticket = operation.change_ticket
        ticket.status = ticket_status

        ticket.save(
            update_fields=["status", "updated_at"]
        )

    return step


@transaction.atomic
def fail_step(
    *,
    step_execution_id,
    actor,
    error_message,
):
    """
    Фиксирует ошибку шага и завершает текущую операцию
    со статусом FAILED.

    Карточка изменения остаётся в работе, чтобы можно было
    принять решение о повторной попытке или откате.
    """

    _require_executor(actor)

    error_message = (error_message or "").strip()

    if not error_message:
        raise OperationServiceError(
            "Необходимо указать причину ошибки."
        )

    operation, step = _lock_operation_and_step(
        step_execution_id
    )

    _require_running_operation(operation)

    if step.status != StepExecutionStatus.IN_PROGRESS:
        raise OperationServiceError(
            "Зафиксировать ошибку можно только "
            "для выполняющегося шага."
        )

    now = timezone.now()

    step.status = StepExecutionStatus.FAILED
    step.finished_by = actor
    step.finished_at = now
    step.error_message = error_message

    step.full_clean()

    step.save(
        update_fields=[
            "status",
            "finished_by",
            "finished_at",
            "error_message",
            "updated_at",
        ]
    )

    operation.status = OperationStatus.FAILED
    operation.finished_at = now
    operation.error_message = error_message

    operation.full_clean()

    operation.save(
        update_fields=[
            "status",
            "finished_at",
            "error_message",
            "updated_at",
        ]
    )

    return step

def start_update_operation(*, ticket_id, actor):
    """
    Запускает операцию обновления.

    Проверяет карточку и пакет, создаёт операцию
    и неизменяемые снимки плановых шагов.
    """

    _require_executor(actor)

    hash_error_message = None
    operation = None

    with transaction.atomic():

        try:
            ticket = (
                ChangeTicket.objects
                .select_for_update()
                .select_related("software", "status")
                .get(pk=ticket_id)
            )
        except ChangeTicket.DoesNotExist:
            raise OperationServiceError(
                "Карточка изменения не найдена."
            )

        # 1. Проверка прав
        if ticket.created_by_id != actor.pk:
            raise PermissionDenied(
                "Запускать обновление может только исполнитель, "
                "создавший карточку."
            )

        if not ticket.software.is_active:
            raise OperationServiceError(
                "Нельзя обновлять неактивное программное обеспечение."
            )

        # 2. Проверка статуса карточки
        status_code = ticket.status.code

        if status_code == CardStatusCode.IN_PROGRESS:
            previous_operation = (
                Operation.objects
                .filter(change_ticket=ticket)
                .order_by("-sequence_number")
                .first()
            )

            if (
                previous_operation is None
                or previous_operation.operation_type != OperationType.UPDATE
                or previous_operation.status != OperationStatus.FAILED
            ):
                raise OperationServiceError(
                    "Повторное обновление разрешено только "
                    "после неудачной попытки обновления."
                )

        elif status_code != CardStatusCode.APPROVED:
            raise OperationServiceError(
                "Запуск возможен только для согласованной карточки."
            )

        if not ticket.approved_by_id:
            raise OperationServiceError(
                "У карточки отсутствует согласование руководителя."
            )

        # 3. Проверка планового окна
        now = timezone.now()

        if not ticket.window_start_at or not ticket.window_end_at:
            raise OperationServiceError(
                "У карточки не задано плановое окно."
            )

        if not ticket.window_start_at <= now <= ticket.window_end_at:
            raise OperationServiceError(
                "Запуск находится за пределами согласованного окна."
            )

        # 4. Проверка предпроверок
        if ticket.compatibility_status != CompatibilityStatus.COMPATIBLE:
            raise OperationServiceError(
                "Совместимость программного обеспечения не подтверждена."
            )

        if ticket.resources_check_status != ResourcesCheckStatus.SUFFICIENT:
            raise OperationServiceError(
                "Достаточность системных ресурсов не подтверждена."
            )

        # 5. Проверка плановых шагов
        plan_steps = list(
            PlanStep.objects
            .filter(ticket=ticket)
            .order_by("step_order")
        )

        if not plan_steps:
            raise OperationServiceError(
                "В карточке отсутствуют плановые технические шаги."
            )

        expected_order = list(range(1, len(plan_steps) + 1))

        actual_order = [
            step.step_order for step in plan_steps
        ]

        if actual_order != expected_order:
            raise OperationServiceError(
                "Порядок плановых шагов должен быть непрерывным."
            )

        if plan_steps[0].step_kind != StepKind.BACKUP:
            raise OperationServiceError(
                "Первым шагом должно быть резервное копирование."
            )

        # 6. Проверка пакета
        if not ticket.package_file:
            raise OperationServiceError(
                "В карточке отсутствует пакет обновления."
            )

        if not ticket.expected_sha256:
            raise OperationServiceError(
                "Не указана ожидаемая SHA-256."
            )

        try:
            actual_sha256 = _calculate_package_sha256(
                ticket.package_file
            )
        except (OSError, ValueError):
            actual_sha256 = ""
            hash_error_message = (
                "Не удалось прочитать пакет обновления. "
                "Операция не запущена."
            )

        ticket.actual_sha256 = actual_sha256
        ticket.hash_checked_at = now

        if hash_error_message:
            ticket.hash_status = HashStatus.MISMATCH

        elif actual_sha256.lower() != ticket.expected_sha256.lower():
            ticket.hash_status = HashStatus.MISMATCH
            hash_error_message = (
                "Контрольная сумма SHA-256 не совпадает. "
                "Операция не создана."
            )

        else:
            ticket.hash_status = HashStatus.MATCH

        # Сохраняем результат проверки независимо от её исхода.
        ticket.save(
            update_fields=[
                "actual_sha256",
                "hash_status",
                "hash_checked_at",
                "updated_at",
            ]
        )

        # 7–10. Создание операции выполняется только
        # при успешной проверке SHA-256.
        if hash_error_message is None:

            if Operation.objects.filter(
                change_ticket=ticket,
                status=OperationStatus.RUNNING,
            ).exists():
                raise OperationServiceError(
                    "Для карточки уже выполняется операция."
                )

            max_sequence = (
                Operation.objects
                .filter(change_ticket=ticket)
                .aggregate(value=Max("sequence_number"))["value"] or 0
            )

            max_attempt = (
                Operation.objects
                .filter(
                    change_ticket=ticket,
                    operation_type=OperationType.UPDATE,
                )
                .aggregate(value=Max("attempt_number"))["value"] or 0
            )

            operation = Operation(
                change_ticket=ticket,
                operation_type=OperationType.UPDATE,
                sequence_number=max_sequence + 1,
                attempt_number=max_attempt + 1,
                initiated_by=actor,
                status=OperationStatus.RUNNING,
                started_at=now,
            )

            operation.full_clean()
            operation.save()

            for index, plan_step in enumerate(plan_steps):

                execution = StepExecution(
                    operation=operation,
                    plan_step=plan_step,
                    step_order=plan_step.step_order,
                    step_kind=plan_step.step_kind,
                    name=plan_step.name,
                    description=plan_step.description,
                    planned_duration_min=plan_step.planned_duration_min,
                    status=(
                        StepExecutionStatus.AVAILABLE
                        if index == 0
                        else StepExecutionStatus.LOCKED
                    ),
                )

                execution.full_clean()
                execution.save()

            # 11. Переводим согласованную карточку в работу.
            if status_code == CardStatusCode.APPROVED:
                ticket.status = CardStatus.objects.get(
                    code=CardStatusCode.IN_PROGRESS
                )

                ticket.save(
                    update_fields=["status", "updated_at"]
                )

    # Исключение выбрасывается уже после завершения транзакции.
    # Поэтому результат неудачной проверки SHA-256 сохранён.
    if hash_error_message:
        raise OperationServiceError(hash_error_message)

    return operation

def start_rollback_operation(
    *,
    parent_operation_id,
    backup_id,
    rollback_reason_id,
    actor,
):
    """
    Запускает операцию отката.

    Проверяет исходную операцию, карточку, причину отката
    и резервную копию.

    Перед созданием операции повторно проверяет SHA-256
    и размер резервной копии.

    Результат неудачной проверки резервной копии сохраняется,
    но операция отката не создаётся.

    Шаги отката формируются из активных StepTemplate
    с operation_type=ROLLBACK.
    """

    from backups.models import Backup
    from backups.services import verify_backup

    _require_executor(actor)

    verification_error_message = None
    operation = None

    with transaction.atomic():

        # ----------------------------------------------------
        # 1. БЛОКИРУЕМ ИСХОДНУЮ ОПЕРАЦИЮ
        # ----------------------------------------------------

        try:
            parent_operation = (
                Operation.objects
                .select_for_update()
                .get(pk=parent_operation_id)
            )
        except Operation.DoesNotExist:
            raise OperationServiceError(
                "Исходная операция не найдена."
            )

        if parent_operation.operation_type != OperationType.UPDATE:
            raise OperationServiceError(
                "Откат возможен только относительно операции обновления."
            )

        if parent_operation.status not in {
            OperationStatus.FAILED,
            OperationStatus.COMPLETED,
        }:
            raise OperationServiceError(
                "Откат возможен только после завершения "
                "или неудачной попытки обновления."
            )

        # ----------------------------------------------------
        # 2. БЛОКИРУЕМ КАРТОЧКУ И ПРОВЕРЯЕМ ПРАВА
        # ----------------------------------------------------

        try:
            ticket = (
                ChangeTicket.objects
                .select_for_update()
                .select_related("status")
                .get(pk=parent_operation.change_ticket_id)
            )
        except ChangeTicket.DoesNotExist:
            raise OperationServiceError(
                "Карточка изменения не найдена."
            )

        if ticket.created_by_id != actor.pk:
            raise PermissionDenied(
                "Запускать откат может только исполнитель, "
                "создавший карточку изменения."
            )

        if ticket.status.code not in {
            CardStatusCode.IN_PROGRESS,
            CardStatusCode.COMPLETED,
        }:
            raise OperationServiceError(
                "Текущий статус карточки не допускает запуск отката."
            )

        # ----------------------------------------------------
        # 3. ПРОВЕРЯЕМ АКТУАЛЬНОСТЬ ИСХОДНОЙ ОПЕРАЦИИ
        # ----------------------------------------------------

        latest_update = (
            Operation.objects
            .filter(
                change_ticket=ticket,
                operation_type=OperationType.UPDATE,
            )
            .order_by("-sequence_number")
            .first()
        )

        if (
            latest_update is None
            or latest_update.pk != parent_operation.pk
        ):
            raise OperationServiceError(
                "Откат разрешён только для последней "
                "операции обновления карточки."
            )

        if parent_operation.rollback_operations.filter(
            status=OperationStatus.COMPLETED
        ).exists():
            raise OperationServiceError(
                "Для этой операции обновления откат уже успешно выполнен."
            )

        if Operation.objects.filter(
            change_ticket=ticket,
            status=OperationStatus.RUNNING,
        ).exists():
            raise OperationServiceError(
                "По этой карточке уже выполняется другая операция."
            )

        # ----------------------------------------------------
        # 4. ПРОВЕРЯЕМ ПРИЧИНУ ОТКАТА
        # ----------------------------------------------------

        try:
            rollback_reason = RollbackReason.objects.get(
                pk=rollback_reason_id,
                is_active=True,
            )
        except RollbackReason.DoesNotExist:
            raise OperationServiceError(
                "Причина отката не найдена или неактивна."
            )

        # ----------------------------------------------------
        # 5. ПОЛУЧАЕМ РЕЗЕРВНУЮ КОПИЮ
        # ----------------------------------------------------

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

        if backup.operation_id != parent_operation.pk:
            raise OperationServiceError(
                "Выбранная резервная копия не относится "
                "к исходной операции обновления."
            )

        # ----------------------------------------------------
        # 6. ПОЛУЧАЕМ ШАБЛОНЫ ШАГОВ ОТКАТА
        # ----------------------------------------------------

        templates = list(
            StepTemplate.objects
            .filter(
                operation_type=OperationType.ROLLBACK,
                is_active=True,
            )
            .order_by("sort_order", "name")
        )

        if not templates:
            raise OperationServiceError(
                "Не найдены активные шаблоны шагов отката. "
                "Сначала необходимо настроить StepTemplate."
            )

        # ----------------------------------------------------
        # 7. ПОВТОРНАЯ ПРОВЕРКА РЕЗЕРВНОЙ КОПИИ
        # ----------------------------------------------------

        # verify_backup использует собственную transaction.atomic.
        # Здесь она выполняется внутри внешней транзакции.
        # Если копия повреждена, мы не выбрасываем исключение
        # внутри транзакции, чтобы сохранить ERROR или INVALID.

        verified_backup = verify_backup(
            backup_id=backup.pk,
            actor=actor,
        )

        if not verified_backup.is_usable:

            verification_error_message = (
                verified_backup.verification_error
                or "Целостность резервной копии не подтверждена."
            )

        else:

            # ------------------------------------------------
            # 8. РАССЧИТЫВАЕМ НОМЕРА ОПЕРАЦИИ И ПОПЫТКИ
            # ------------------------------------------------

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

            now = timezone.now()

            # ------------------------------------------------
            # 9. СОЗДАЁМ ОПЕРАЦИЮ ОТКАТА
            # ------------------------------------------------

            operation = Operation(
                change_ticket=ticket,
                operation_type=OperationType.ROLLBACK,
                sequence_number=max_sequence + 1,
                attempt_number=max_attempt + 1,
                parent_operation=parent_operation,
                backup_used=verified_backup,
                rollback_reason=rollback_reason,
                initiated_by=actor,
                status=OperationStatus.RUNNING,
                started_at=now,
            )

            operation.full_clean()
            operation.save()

            # ------------------------------------------------
            # 10. СОЗДАЁМ СОБСТВЕННЫЕ ШАГИ ОТКАТА
            # ------------------------------------------------

            for index, template in enumerate(templates):

                execution = StepExecution(
                    operation=operation,
                    plan_step=None,
                    step_order=index + 1,
                    step_kind=template.step_kind,
                    name=template.name,
                    description=template.description,
                    planned_duration_min=template.default_duration_min,
                    status=(
                        StepExecutionStatus.AVAILABLE
                        if index == 0
                        else StepExecutionStatus.LOCKED
                    ),
                )

                execution.full_clean()
                execution.save()

            # ------------------------------------------------
            # 11. ВОЗВРАЩАЕМ КАРТОЧКУ В РАБОТУ
            # ------------------------------------------------

            if ticket.status.code == CardStatusCode.COMPLETED:

                ticket.status = CardStatus.objects.get(
                    code=CardStatusCode.IN_PROGRESS
                )

                ticket.save(
                    update_fields=["status", "updated_at"]
                )

    # --------------------------------------------------------
    # 12. СООБЩАЕМ ОБ ОШИБКЕ ПОСЛЕ ЗАВЕРШЕНИЯ ТРАНЗАКЦИИ
    # --------------------------------------------------------

    if verification_error_message:
        raise OperationServiceError(
            "Резервная копия не прошла проверку целостности. "
            + verification_error_message
        )

    return operation
