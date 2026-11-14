
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from access.models import ModuleRole, RoleAssignment
from audit.services import AuditAction, record_event
from catalogs.models import CardStatus, CardStatusCode

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeTicket,
    PlanStep,
    StepKind,
)


class ChangeWorkflowError(ValidationError):
    """Ошибка нарушения правил бизнес-процесса."""
    pass


def _require_role(user, role):
    """Проверяет активность пользователя и его роль в модуле."""

    if (
        not user
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        raise PermissionDenied("Необходимо войти в систему.")

    has_role = RoleAssignment.objects.filter(
        user=user,
        role=role,
    ).exists()

    if not has_role:
        raise PermissionDenied(
            "У вас нет необходимой роли для этого действия."
        )


def _require_ticket_creator(actor, ticket):
    """Разрешает исполнителю работать только со своей карточкой."""

    if ticket.created_by_id != actor.pk:
        raise PermissionDenied(
            "Вы можете изменять только созданные вами карточки."
        )


def _require_not_creator(actor, ticket):
    """Запрещает руководителю согласовывать собственную карточку."""

    if ticket.created_by_id == actor.pk:
        raise PermissionDenied(
            "Нельзя принимать решение по собственной карточке."
        )


def _get_status(code):
    """Получает системный статус из справочника."""

    try:
        return CardStatus.objects.get(code=code)
    except CardStatus.DoesNotExist:
        raise ChangeWorkflowError(
            f"Системный статус '{code}' отсутствует в справочнике."
        )


def _get_locked_ticket(ticket_id):
    """Получает карточку с блокировкой строки на время транзакции."""

    try:
        return (
            ChangeTicket.objects
            .select_for_update()
            .select_related("status", "software")
            .get(pk=ticket_id)
        )
    except ChangeTicket.DoesNotExist:
        raise ChangeWorkflowError("Карточка изменения не найдена.")


def _validate_new_window(start_at, end_at):
    """Проверяет новые даты планового окна."""

    if start_at is None or end_at is None:
        raise ChangeWorkflowError(
            "Необходимо указать начало и окончание планового окна."
        )

    if start_at >= end_at:
        raise ChangeWorkflowError(
            "Окончание планового окна должно быть позже начала."
        )


# ---------------------------------------------------------
# СОЗДАНИЕ КАРТОЧКИ
# ---------------------------------------------------------

@transaction.atomic
def create_draft(
    *,
    actor,
    software,
    source_version="",
    target_version="",
    risks="",
    window_start_at=None,
    window_end_at=None,
):
    """Создаёт новую карточку в статусе draft."""

    _require_role(actor, ModuleRole.EXECUTOR)

    if not software.is_active:
        raise ChangeWorkflowError(
            "Нельзя создать карточку для неактивного ПО."
        )

    draft_status = _get_status(CardStatusCode.DRAFT)

    ticket = ChangeTicket(
        software=software,
        created_by=actor,
        status=draft_status,
        source_version=source_version,
        target_version=target_version,
        risks=risks,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )

    ticket.full_clean()
    ticket.save()

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_CREATED,
        obj=ticket,
        details={
            "software_id": str(ticket.software_id),
            "status": ticket.status.code,
            "source_version": ticket.source_version,
            "target_version": ticket.target_version,
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТПРАВКА НА СОГЛАСОВАНИЕ
# ---------------------------------------------------------

@transaction.atomic
def submit_for_approval(*, ticket_id, actor):
    """Отправляет собственный черновик на согласование."""

    _require_role(actor, ModuleRole.EXECUTOR)

    ticket = _get_locked_ticket(ticket_id)

    _require_ticket_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.DRAFT:
        raise ChangeWorkflowError(
            "На согласование можно отправить только черновик."
        )

    if not ticket.software.is_active:
        raise ChangeWorkflowError(
            "Программное обеспечение неактивно."
        )

    _validate_new_window(
        ticket.window_start_at,
        ticket.window_end_at,
    )

    ticket.clean()

    has_backup_step = PlanStep.objects.filter(
        ticket=ticket,
        step_order=1,
        step_kind=StepKind.BACKUP,
    ).exists()

    if not has_backup_step:
        raise ChangeWorkflowError(
            "Первым шагом должно быть резервное копирование."
        )

    ticket.status = _get_status(CardStatusCode.PENDING)
    ticket.rejection_comment = ""

    ticket.save(
        update_fields=[
            "status",
            "rejection_comment",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_SUBMITTED,
        obj=ticket,
        details={
            "status_before": CardStatusCode.DRAFT,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# СОГЛАСОВАНИЕ
# ---------------------------------------------------------

@transaction.atomic
def approve_ticket(*, ticket_id, actor, comment=""):
    """Согласовывает карточку руководителем."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.PENDING:
        raise ChangeWorkflowError(
            "Согласовать можно только карточку на согласовании."
        )

    old_status = ticket.status

    ticket.status = _get_status(CardStatusCode.APPROVED)
    ticket.approved_by = actor
    ticket.rejection_comment = ""

    ticket.save(
        update_fields=[
            "status",
            "approved_by",
            "rejection_comment",
            "updated_at",
        ]
    )

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.APPROVED,
        comment=comment.strip(),
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_APPROVED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": old_status.code,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТКЛОНЕНИЕ
# ---------------------------------------------------------

@transaction.atomic
def reject_ticket(*, ticket_id, actor, comment):
    """Отклоняет карточку и возвращает её в черновик."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.PENDING:
        raise ChangeWorkflowError(
            "Отклонить можно только карточку на согласовании."
        )

    comment = comment.strip()

    if not comment:
        raise ChangeWorkflowError(
            "При отклонении необходимо указать комментарий."
        )

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.REJECTED,
        comment=comment,
    )

    ticket.status = _get_status(CardStatusCode.DRAFT)
    ticket.rejection_comment = comment
    ticket.approved_by = None

    ticket.save(
        update_fields=[
            "status",
            "rejection_comment",
            "approved_by",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_REJECTED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": CardStatusCode.PENDING,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# ПЕРЕНОС ПЛАНОВОГО ОКНА
# ---------------------------------------------------------

@transaction.atomic
def reschedule_ticket(
    *,
    ticket_id,
    actor,
    new_start_at,
    new_end_at,
    comment="",
):
    """
    Переносит плановое окно.

    pending -> approved
    approved -> approved

    Перенос обязательно фиксируется в ApprovalRecord.
    """

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    current_status = ticket.status.code

    if current_status not in {
        CardStatusCode.PENDING,
        CardStatusCode.APPROVED,
    }:
        raise ChangeWorkflowError(
            "Перенести окно можно только у карточки "
            "на согласовании или уже согласованной карточки."
        )

    _validate_new_window(new_start_at, new_end_at)

    old_start_at = ticket.window_start_at
    old_end_at = ticket.window_end_at

    ticket.window_start_at = new_start_at
    ticket.window_end_at = new_end_at

    ticket.clean()

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.RESCHEDULED,
        comment=comment.strip(),
        old_start_at=old_start_at,
        old_end_at=old_end_at,
        new_start_at=new_start_at,
        new_end_at=new_end_at,
    )

    update_fields = [
        "window_start_at",
        "window_end_at",
        "updated_at",
    ]

    if current_status == CardStatusCode.PENDING:
        ticket.status = _get_status(CardStatusCode.APPROVED)
        ticket.approved_by = actor

        update_fields.extend([
            "status",
            "approved_by",
        ])

    ticket.save(update_fields=update_fields)

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_RESCHEDULED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": current_status,
            "status_after": ticket.status.code,
            "old_window_start": (
                old_start_at.isoformat()
                if old_start_at else None
            ),
            "old_window_end": (
                old_end_at.isoformat()
                if old_end_at else None
            ),
            "new_window_start": new_start_at.isoformat(),
            "new_window_end": new_end_at.isoformat(),
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТМЕНА
# ---------------------------------------------------------

@transaction.atomic
def cancel_ticket(*, ticket_id, actor, comment=""):
    """Отменяет карточку на согласовании или согласованную."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code not in {
        CardStatusCode.PENDING,
        CardStatusCode.APPROVED,
    }:
        raise ChangeWorkflowError(
            "Отменить можно только карточку на согласовании "
            "или согласованную карточку."
        )

    old_status = ticket.status.code

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.CANCELLED,
        comment=comment.strip(),
        old_start_at=ticket.window_start_at,
        old_end_at=ticket.window_end_at,
    )

    ticket.status = _get_status(CardStatusCode.CANCELLED)

    ticket.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_CANCELLED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": old_status,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# РЕДАКТИРОВАНИЕ ЧЕРНОВИКА
# ---------------------------------------------------------

@transaction.atomic
def update_draft(
    *,
    ticket_id,
    actor,
    software,
    source_version="",
    target_version="",
    risks="",
    window_start_at=None,
    window_end_at=None,
):
    """
    Обновляет существующий черновик.

    Исполнитель может редактировать только собственные
    карточки, пока они находятся в статусе draft.
    """

    _require_role(actor, ModuleRole.EXECUTOR)

    ticket = _get_locked_ticket(ticket_id)

    _require_ticket_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.DRAFT:
        raise ChangeWorkflowError(
            "Редактировать можно только карточку в статусе «Черновик»."
        )

    changed_fields = []

    if ticket.software_id != software.pk:
        changed_fields.append("software")

    if ticket.source_version != source_version:
        changed_fields.append("source_version")

    if ticket.target_version != target_version:
        changed_fields.append("target_version")

    if ticket.risks != risks:
        changed_fields.append("risks")

    if ticket.window_start_at != window_start_at:
        changed_fields.append("window_start_at")

    if ticket.window_end_at != window_end_at:
        changed_fields.append("window_end_at")

    if not software.is_active and software.pk != ticket.software_id:
        raise ChangeWorkflowError(
            "Нельзя выбрать неактивное программное обеспечение."
        )

    ticket.software = software
    ticket.source_version = source_version
    ticket.target_version = target_version
    ticket.risks = risks
    ticket.window_start_at = window_start_at
    ticket.window_end_at = window_end_at

    ticket.full_clean()

    ticket.save(
        update_fields=[
            "software",
            "source_version",
            "target_version",
            "risks",
            "window_start_at",
            "window_end_at",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_UPDATED,
        obj=ticket,
        details={
            "changed_fields": changed_fields,
            "status": ticket.status.code,
        },
    )

    return ticket
