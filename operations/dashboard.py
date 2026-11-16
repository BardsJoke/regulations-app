from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.views.decorators.http import require_GET, require_POST

from wagtail.admin.menu import MenuItem
from wagtail.admin.viewsets.base import ViewSet

from access.models import ModuleRole, RoleAssignment
from backups.models import Backup, BackupIntegrityStatus
from catalogs.models import (
    CardStatusCode,
    RollbackReason,
)
from changes.models import ChangeTicket, StepKind
from changes.permissions import ChangeTicketPermissionPolicy

from . import services as operation_services
from .models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
    StepTemplate,
)
from .runner import execute_step


# ==========================================================
# ПОЛИТИКА ПРОСМОТРА КАРТОЧЕК
# ==========================================================

change_ticket_policy = ChangeTicketPermissionPolicy(
    ChangeTicket,
)


def _has_role(user, role):
    """
    Проверяет наличие назначенной роли.
    """

    return (
        user.is_authenticated
        and user.is_active
        and RoleAssignment.objects.filter(
            user=user,
            role=role,
        ).exists()
    )


def _is_executor(user):
    return _has_role(
        user,
        ModuleRole.EXECUTOR,
    )


def _visible_tickets(user):
    """
    Возвращает карточки, доступные пользователю
    согласно существующей политике доступа.
    """

    return (
        change_ticket_policy
        .instances_user_has_permission_for(user, "view")
        .select_related(
            "software",
            "status",
            "created_by",
            "approved_by",
            "deployment_target",
        )
    )


def _visible_operations(user):
    """
    Возвращает операции только по доступным пользователю карточкам.
    """

    visible_ticket_ids = _visible_tickets(user).values("pk")

    return (
        Operation.objects
        .filter(
            change_ticket_id__in=visible_ticket_ids,
        )
        .select_related(
            "change_ticket",
            "change_ticket__software",
            "change_ticket__status",
            "change_ticket__created_by",
            "change_ticket__deployment_target",
            "initiated_by",
            "parent_operation",
            "backup_used",
            "rollback_reason",
        )
        .prefetch_related("steps")
    )


def _visible_step(user, step_id):
    """
    Проверяет доступ к шагу через доступность его операции.
    """

    visible_operation_ids = (
        _visible_operations(user)
        .values("pk")
    )

    return get_object_or_404(
        StepExecution.objects.select_related(
            "operation",
            "operation__change_ticket",
        ),
        pk=step_id,
        operation_id__in=visible_operation_ids,
    )


def _error_text(error):
    """
    Преобразует исключение в понятное сообщение.
    """

    if isinstance(error, ValidationError):
        error_messages = getattr(error, "messages", None)

        if error_messages:
            return "; ".join(
                str(item)
                for item in error_messages
            )

    return str(error)


def _operation_url(operation):
    return reverse(
        "operations_dashboard:detail",
        args=[operation.pk],
    )


# ==========================================================
# ОСНОВНАЯ СТРАНИЦА ОПЕРАЦИЙ
# ==========================================================

@require_GET
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def operations_index(request):
    """
    Показывает доступные пользователю операции
    и согласованные карточки, ожидающие запуска обновления.
    """

    tickets = _visible_tickets(request.user)

    operations = list(
        _visible_operations(request.user)
        .order_by("-started_at")[:100]
    )

    can_start_updates = _is_executor(request.user)

    if can_start_updates:
        approved_tickets = list(
            tickets.filter(
                created_by=request.user,
                status__code=CardStatusCode.APPROVED,
                approved_by__isnull=False,
            ).order_by("-created_at")
        )
    else:
        approved_tickets = []

    context = {
        "operations": operations,
        "approved_tickets": approved_tickets,
        "can_start_updates": can_start_updates,
    }

    return render(
        request,
        "operations/dashboard_index.html",
        context,
    )


# ==========================================================
# ПОДРОБНАЯ СТРАНИЦА ОПЕРАЦИИ
# ==========================================================

@require_GET
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def operation_detail(request, operation_id):
    """
    Показывает информацию об операции,
    её шагах, результатах и доступных действиях.
    """

    operation = get_object_or_404(
        _visible_operations(request.user),
        pk=operation_id,
    )

    ticket = operation.change_ticket

    steps = list(
        operation.steps.all().order_by("step_order")
    )

    is_executor_owner = (
        _is_executor(request.user)
        and ticket.created_by_id == request.user.pk
    )

    can_execute_steps = (
        is_executor_owner
        and operation.status == OperationStatus.RUNNING
    )

    # ------------------------------------------------------
    # Возможность начать откат
    # ------------------------------------------------------

    rollback_backups = Backup.objects.none()
    rollback_reasons = RollbackReason.objects.none()

    can_start_rollback = False
    rollback_templates_ready = False

    if (
        is_executor_owner
        and operation.operation_type == OperationType.UPDATE
        and operation.status in {
            OperationStatus.FAILED,
            OperationStatus.COMPLETED,
        }
    ):
        latest_update = (
            Operation.objects
            .filter(
                change_ticket=ticket,
                operation_type=OperationType.UPDATE,
            )
            .order_by("-attempt_number")
            .first()
        )

        has_completed_rollback = (
            Operation.objects.filter(
                change_ticket=ticket,
                operation_type=OperationType.ROLLBACK,
                status=OperationStatus.COMPLETED,
            ).exists()
        )

        ticket_allows_rollback = (
            ticket.status.code in {
                CardStatusCode.IN_PROGRESS,
                CardStatusCode.COMPLETED,
            }
        )

        rollback_backups = (
            Backup.objects
            .filter(
                operation=operation,
                integrity_status=BackupIntegrityStatus.VALID,
                verified_at__isnull=False,
            )
            .order_by("-created_at")
        )

        rollback_reasons = RollbackReason.objects.filter(
            is_active=True,
        )

        rollback_templates_ready = StepTemplate.objects.filter(
            operation_type=OperationType.ROLLBACK,
            is_active=True,
        ).exists()

        can_start_rollback = (
            latest_update is not None
            and latest_update.pk == operation.pk
            and not has_completed_rollback
            and ticket_allows_rollback
            and rollback_backups.exists()
            and rollback_reasons.exists()
            and rollback_templates_ready
        )

    context = {
        "operation": operation,
        "ticket": ticket,
        "steps": steps,
        "is_executor_owner": is_executor_owner,
        "can_execute_steps": can_execute_steps,
        "can_start_rollback": can_start_rollback,
        "rollback_backups": rollback_backups,
        "rollback_reasons": rollback_reasons,
        "rollback_templates_ready": rollback_templates_ready,
        "ticket_url": reverse(
            "change_tickets:inspect",
            args=[ticket.pk],
        ),
    }

    return render(
        request,
        "operations/dashboard_detail.html",
        context,
    )


# ==========================================================
# ЗАПУСК ОПЕРАЦИИ ОБНОВЛЕНИЯ
# ==========================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def start_update_action(request, ticket_id):
    """
    Создаёт операцию обновления
    по согласованной карточке.
    """

    ticket = get_object_or_404(
        _visible_tickets(request.user),
        pk=ticket_id,
    )

    try:
        operation = operation_services.start_update_operation(
            ticket_id=ticket.pk,
            actor=request.user,
        )

    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            _error_text(error),
        )

        return redirect("operations_dashboard:index")

    messages.success(
        request,
        "Операция обновления создана. "
        "Можно приступать к первому шагу.",
    )

    return redirect(
        _operation_url(operation)
    )


# ==========================================================
# ВЫПОЛНЕНИЕ АВТОМАТИЧЕСКОГО ШАГА
# ==========================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def execute_step_action(request, step_id):
    """
    Выполняет автоматический шаг через LocalFilesystemAdapter.
    """

    step = _visible_step(
        request.user,
        step_id,
    )

    operation_id = step.operation_id

    try:
        result = execute_step(
            step_execution_id=step.pk,
            actor=request.user,
        )

    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            _error_text(error),
        )

    else:
        if result.status == StepExecutionStatus.COMPLETED:
            messages.success(
                request,
                "Шаг успешно выполнен.",
            )

        elif result.status == StepExecutionStatus.FAILED:
            messages.error(
                request,
                result.error_message or "При выполнении шага произошла ошибка.",
            )

        else:
            messages.warning(
                request,
                "Шаг не завершён. Проверьте его текущий статус.",
            )

    return redirect(
        "operations_dashboard:detail",
        operation_id=operation_id,
    )


# ==========================================================
# НАЧАЛО РУЧНОГО ШАГА
# ==========================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def start_manual_step_action(request, step_id):
    """
    Начинает ручной шаг типа OTHER.
    """

    step = _visible_step(
        request.user,
        step_id,
    )

    operation_id = step.operation_id

    try:
        if step.step_kind != StepKind.OTHER:
            raise ValidationError(
                "Через ручной режим можно выполнять только шаги типа «Прочее»."
            )

        operation_services.start_step(
            step_execution_id=step.pk,
            actor=request.user,
        )

    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            _error_text(error),
        )

    else:
        messages.success(
            request,
            "Ручной шаг переведён в состояние «Выполняется».",
        )

    return redirect(
        "operations_dashboard:detail",
        operation_id=operation_id,
    )


# ==========================================================
# ЗАВЕРШЕНИЕ РУЧНОГО ШАГА
# ==========================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def complete_manual_step_action(request, step_id):
    """
    Завершает ручной шаг с обязательным описанием результата.
    """

    step = _visible_step(
        request.user,
        step_id,
    )

    operation_id = step.operation_id

    result = request.POST.get("result", "").strip()

    try:
        if step.step_kind != StepKind.OTHER:
            raise ValidationError(
                "Через ручной режим можно завершать только шаги типа «Прочее»."
            )

        if not result:
            raise ValidationError(
                "Укажите результат выполнения ручного шага."
            )

        operation_services.complete_step(
            step_execution_id=step.pk,
            actor=request.user,
            result=result,
        )

    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            _error_text(error),
        )

    else:
        messages.success(
            request,
            "Ручной шаг завершён.",
        )

    return redirect(
        "operations_dashboard:detail",
        operation_id=operation_id,
    )


# ==========================================================
# ЗАПУСК ОТКАТА
# ==========================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def start_rollback_action(request, operation_id):
    """
    Запускает откат с выбором резервной копии и причины.
    """

    parent_operation = get_object_or_404(
        _visible_operations(request.user),
        pk=operation_id,
    )

    backup_id = request.POST.get("backup_id", "").strip()
    rollback_reason_id = request.POST.get(
        "rollback_reason_id",
        "",
    ).strip()

    if not backup_id or not rollback_reason_id:
        messages.error(
            request,
            "Выберите резервную копию и причину отката.",
        )

        return redirect(
            "operations_dashboard:detail",
            operation_id=parent_operation.pk,
        )

    try:
        rollback_operation = (
            operation_services.start_rollback_operation(
                parent_operation_id=parent_operation.pk,
                backup_id=backup_id,
                rollback_reason_id=rollback_reason_id,
                actor=request.user,
            )
        )

    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            _error_text(error),
        )

        return redirect(
            "operations_dashboard:detail",
            operation_id=parent_operation.pk,
        )

    messages.success(
        request,
        "Операция отката создана. "
        "Можно приступать к восстановлению.",
    )

    return redirect(
        _operation_url(rollback_operation)
    )


# ==========================================================
# ПУНКТ МЕНЮ
# ==========================================================

class OperationsDashboardMenuItem(MenuItem):
    """
    Раздел доступен только пользователям
    с одной из ролей модуля.
    """

    def is_shown(self, request):
        user = request.user

        if not user.is_authenticated or not user.is_active:
            return False

        return RoleAssignment.objects.filter(
            user=user,
            role__in=[
                ModuleRole.ADMINISTRATOR,
                ModuleRole.APPROVER,
                ModuleRole.EXECUTOR,
            ],
        ).exists()


# ==========================================================
# VIEWSET
# ==========================================================

class OperationsDashboardViewSet(ViewSet):

    name = "operations_dashboard"

    menu_label = "Операции"
    icon = "list-ul"

    add_to_admin_menu = True
    menu_item_class = OperationsDashboardMenuItem
    menu_order = 325

    def get_urlpatterns(self):
        return [
            path(
                "",
                operations_index,
                name="index",
            ),
            path(
                "operation/<uuid:operation_id>/",
                operation_detail,
                name="detail",
            ),
            path(
                "ticket/<uuid:ticket_id>/start-update/",
                start_update_action,
                name="start_update",
            ),
            path(
                "step/<uuid:step_id>/execute/",
                execute_step_action,
                name="execute_step",
            ),
            path(
                "step/<uuid:step_id>/start-manual/",
                start_manual_step_action,
                name="start_manual_step",
            ),
            path(
                "step/<uuid:step_id>/complete-manual/",
                complete_manual_step_action,
                name="complete_manual_step",
            ),
            path(
                "operation/<uuid:operation_id>/start-rollback/",
                start_rollback_action,
                name="start_rollback",
            ),
        ]