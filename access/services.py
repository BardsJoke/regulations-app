
from django.core.exceptions import ValidationError
from django.db import transaction

from audit.services import AuditAction, record_event

from .models import ModuleRole, RoleAssignment


class RoleAssignmentWorkflowError(ValidationError):
    """Ошибка нарушения правил управления ролями."""
    pass


def _require_saved_assignment(assignment):
    """Проверяет, что назначение уже сохранено в базе."""

    if assignment.pk is None or assignment._state.adding:
        raise RoleAssignmentWorkflowError(
            "Нельзя записать аудит для несохранённого назначения роли."
        )


def _ensure_another_admin_exists(assignment):
    """
    Не позволяет удалить или снять роль с последнего администратора.

    Блокирует существующие назначения администраторов
    в стабильном порядке, чтобы снизить риск гонок.
    """

    using = assignment._state.db or "default"

    admin_ids = list(
        RoleAssignment.objects.using(using)
        .select_for_update()
        .filter(role=ModuleRole.ADMINISTRATOR)
        .order_by("pk")
        .values_list("pk", flat=True)
    )

    has_another_admin = any(
        assignment_id != assignment.pk
        for assignment_id in admin_ids
    )

    if not has_another_admin:
        raise RoleAssignmentWorkflowError(
            "Нельзя удалить или снять роль с последнего администратора. "
            "Сначала назначьте другого пользователя администратором."
        )


def _assignment_details(
    *,
    assignment_id,
    user_id,
    role,
    assigned_by_id=None,
):
    """Формирует сериализуемые сведения о назначении роли."""

    return {
        "assignment_id": str(assignment_id),
        "subject_user_id": str(user_id),
        "role": role,
        "assigned_by_id": (
            str(assigned_by_id)
            if assigned_by_id is not None
            else None
        ),
    }


@transaction.atomic
def record_role_assigned(*, assignment, actor):
    """Фиксирует успешное назначение роли."""

    _require_saved_assignment(assignment)

    return record_event(
        actor=actor,
        action=AuditAction.ROLE_ASSIGNED,
        obj=assignment,
        details=_assignment_details(
            assignment_id=assignment.pk,
            user_id=assignment.user_id,
            role=assignment.role,
            assigned_by_id=assignment.assigned_by_id,
        ),
    )


@transaction.atomic
def record_role_assignment_change(
    *,
    assignment,
    actor,
    old_user_id,
    old_role,
):
    """
    Фиксирует изменение существующего назначения.

    Изменение пользователя или роли отражается двумя событиями:
    отзыв прежнего назначения и назначение нового.
    """

    _require_saved_assignment(assignment)

    if (
        old_user_id == assignment.user_id
        and old_role == assignment.role
    ):
        return []

    # Если снимается роль администратора,
    # должен остаться хотя бы один другой администратор.
    if (
        old_role == ModuleRole.ADMINISTRATOR
        and assignment.role != ModuleRole.ADMINISTRATOR
    ):
        _ensure_another_admin_exists(assignment)

    revoked_event = record_event(
        actor=actor,
        action=AuditAction.ROLE_REVOKED,
        obj=assignment,
        details={
            **_assignment_details(
                assignment_id=assignment.pk,
                user_id=old_user_id,
                role=old_role,
                assigned_by_id=assignment.assigned_by_id,
            ),
            "change_type": "assignment_updated",
            "new_user_id": str(assignment.user_id),
            "new_role": assignment.role,
        },
    )

    assigned_event = record_event(
        actor=actor,
        action=AuditAction.ROLE_ASSIGNED,
        obj=assignment,
        details={
            **_assignment_details(
                assignment_id=assignment.pk,
                user_id=assignment.user_id,
                role=assignment.role,
                assigned_by_id=assignment.assigned_by_id,
            ),
            "change_type": "assignment_updated",
            "previous_user_id": str(old_user_id),
            "previous_role": old_role,
        },
    )

    return [revoked_event, assigned_event]


@transaction.atomic
def record_role_revoked(*, assignment, actor):
    """
    Проверяет возможность отзыва и фиксирует событие.

    Само удаление выполняет Wagtail DeleteView.
    Вызов должен происходить в той же транзакции,
    что и удаление назначения.
    """

    _require_saved_assignment(assignment)

    if assignment.role == ModuleRole.ADMINISTRATOR:
        _ensure_another_admin_exists(assignment)

    return record_event(
        actor=actor,
        action=AuditAction.ROLE_REVOKED,
        obj=assignment,
        details=_assignment_details(
            assignment_id=assignment.pk,
            user_id=assignment.user_id,
            role=assignment.role,
            assigned_by_id=assignment.assigned_by_id,
        ),
    )
