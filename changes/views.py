from django.contrib.admin.utils import quote
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.views.decorators.http import require_POST

from wagtail.admin import messages
from wagtail.admin.menu import MenuItem
from wagtail.admin.views import generic
from wagtail.admin.viewsets.model import ModelViewSet

from .forms import ChangeTicketForm
from .models import ChangeTicket, PlanStep
from .plan_forms import PlanStepForm
from .permissions import (
    CardStatusCode,
    ChangeTicketPermissionPolicy,
    ModuleRole,
    RoleAssignment,
)
from .services import (
    approve_ticket,
    create_draft,
    reject_ticket,
    submit_for_approval,
    update_draft,
    add_plan_step,
    update_plan_step,
    delete_plan_step,
)


# ============================================================
# ЕДИНАЯ ПОЛИТИКА ДОСТУПА
# ============================================================

change_ticket_policy = ChangeTicketPermissionPolicy(
    ChangeTicket
)


def _user_has_role(user, role):
    """Проверяет наличие конкретной роли у пользователя."""

    if not user or not user.is_authenticated or not user.is_active:
        return False

    return RoleAssignment.objects.filter(
        user_id=user.pk,
        role=role,
    ).exists()


def _get_visible_tickets(user, action="view"):
    """Возвращает карточки, доступные пользователю для действия."""

    return change_ticket_policy.instances_user_has_permission_for(
        user,
        action,
    )


def _get_action_error_message(error):
    """Преобразует ValidationError в читаемое сообщение."""

    return " ".join(str(message) for message in error.messages)


# ============================================================
# ПУНКТ МЕНЮ
# ============================================================

class ChangeTicketMenuItem(MenuItem):
    """
    Показывает раздел карточек пользователям,
    имеющим право просмотра или создания.
    """

    def is_shown(self, request):
        return change_ticket_policy.user_has_any_permission(
            request.user,
            {"view", "add"},
        )


# ============================================================
# СПИСОК КАРТОЧЕК
# ============================================================

class ChangeTicketIndexView(generic.IndexView):
    """
    Администратор:
        Видит все карточки.

    Руководитель:
        Видит карточки на согласовании и согласованные.

    Исполнитель:
        Видит только собственные карточки.
    """

    def get_base_queryset(self):
        return (
            _get_visible_tickets(self.request.user, "view")
            .select_related(
                "software",
                "deployment_target",
                "software_package",
                "status",
                "created_by",
                "approved_by",
            )
        )


# ============================================================
# СОЗДАНИЕ КАРТОЧКИ
# ============================================================

class ChangeTicketCreateView(generic.CreateView):
    """
    Создание новой карточки через сервис create_draft.

    Создавать карточки может исполнитель.
    Проверка роли выполняется также внутри сервиса.
    """

    def save_instance(self):
        return create_draft(
            actor=self.request.user,
            **self.form.cleaned_data,
        )

    def form_valid(self, form):
        try:
            return super().form_valid(form)

        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)


# ============================================================
# РЕДАКТИРОВАНИЕ КАРТОЧКИ
# ============================================================

class ChangeTicketEditView(generic.EditView):
    """
    Редактирование только собственного черновика исполнителя.

    Доступ проверяется до отображения формы.
    Сервис update_draft дополнительно проверяет права
    непосредственно перед изменением записи.
    """

    def get_object(self, queryset=None):
        permitted_tickets = _get_visible_tickets(
            self.request.user,
            "change",
        )

        return get_object_or_404(
            permitted_tickets,
            pk=self.object_pk,
        )

    def form_valid(self, form):
        try:
            self.object = update_draft(
                ticket_id=self.object.pk,
                actor=self.request.user,
                **form.cleaned_data,
            )

        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)

        messages.success(
            self.request,
            "Черновик карточки успешно сохранён.",
        )

        return redirect(self.get_success_url())


# ============================================================
# ПРОСМОТР КАРТОЧКИ
# ============================================================

class ChangeTicketInspectView(generic.InspectView):
    """
    Просмотр конкретной карточки с проверкой прав на запись.

    Чужие черновики исполнителю недоступны даже по UUID.
    """

    template_name = "changes/change_ticket_inspect.html"

    def get_object(self, queryset=None):
        permitted_tickets = _get_visible_tickets(
            self.request.user,
            "view",
        )

        return get_object_or_404(
            permitted_tickets,
            pk=self.pk,
        )

    def get_edit_url(self):
        if not self.edit_url_name:
            return None

        if not change_ticket_policy.user_has_permission_for_instance(
            self.request.user,
            "change",
            self.object,
        ):
            return None

        return reverse(
            self.edit_url_name,
            args=(quote(self.object.pk),),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        ticket = self.object
        user = self.request.user

        is_owner = ticket.created_by_id == user.pk

        is_draft = (
            ticket.status.code == CardStatusCode.DRAFT
        )

        is_pending = (
            ticket.status.code == CardStatusCode.PENDING
        )

        # Исполнитель может отправлять только собственный черновик.
        context["can_submit_for_approval"] = (
            change_ticket_policy.user_has_permission_for_instance(
                user,
                "change",
                ticket,
            )
            and is_owner
            and is_draft
        )

        # Руководитель может принять решение только по чужой
        # карточке, которая ожидает согласования.
        is_approver = _user_has_role(
            user,
            ModuleRole.APPROVER,
        )

        context["can_approve_ticket"] = (
            is_approver
            and not is_owner
            and is_pending
        )

        context["can_reject_ticket"] = (
            is_approver
            and not is_owner
            and is_pending
        )

        context["submit_for_approval_url"] = reverse(
            "change_tickets:submit_for_approval",
            args=[ticket.pk],
        )

        context["approve_ticket_url"] = reverse(
            "change_tickets:approve",
            args=[ticket.pk],
        )

        context["reject_ticket_url"] = reverse(
            "change_tickets:reject",
            args=[ticket.pk],
        )

        # ----------------------------------------------------
        # ПЛАН ИЗМЕНЕНИЙ
        # ----------------------------------------------------

        steps = list(
            ticket.plan_steps.all()
        )

        context["plan_steps"] = steps

        # Изменять план может только автор собственного черновика.
        context["can_manage_plan"] = (
            change_ticket_policy.user_has_permission_for_instance(
                user,
                "change",
                ticket,
            )
        )

        context["plan_add_form"] = PlanStepForm(
            ticket=ticket
        )

        context["plan_add_url"] = reverse(
            "change_tickets:plan_step_add",
            args=[ticket.pk],
        )

        plan_step_items = []

        for index, step in enumerate(steps):
            plan_step_items.append({
                "step": step,

                "form": PlanStepForm(
                    ticket=ticket,
                    step=step,
                ),

                "update_url": reverse(
                    "change_tickets:plan_step_update",
                    args=[ticket.pk, step.pk],
                ),

                "delete_url": reverse(
                    "change_tickets:plan_step_delete",
                    args=[ticket.pk, step.pk],
                ),

                # Удалять разрешается только последний шаг.
                "is_last": index == len(steps) - 1,
            })

        context["plan_step_items"] = plan_step_items

        return context


# ============================================================
# ИСТОРИЯ КАРТОЧКИ
# ============================================================

class ChangeTicketHistoryView(generic.HistoryView):
    """
    История доступна пользователям, имеющим право просмотра
    соответствующей карточки.
    """

    any_permission_required = ["view"]

    def get_base_object_queryset(self):
        return _get_visible_tickets(
            self.request.user,
            "view",
        )

    def get_edit_url(self, instance):
        if not change_ticket_policy.user_has_permission_for_instance(
            self.request.user,
            "change",
            instance,
        ):
            return None

        if not self.edit_url_name:
            return None

        return reverse(
            self.edit_url_name,
            args=(quote(instance.pk),),
        )

    @property
    def header_buttons(self):
        if not change_ticket_policy.user_has_permission_for_instance(
            self.request.user,
            "change",
            self.object,
        ):
            return []

        return super().header_buttons


# ============================================================
# ИСПОЛЬЗОВАНИЕ КАРТОЧКИ
# ============================================================

class ChangeTicketUsageView(generic.UsageView):
    """
    Информация об использовании доступных карточек.
    """

    permission_required = "view"

    def get_base_object_queryset(self):
        return _get_visible_tickets(
            self.request.user,
            "view",
        )

    def get_edit_url(self, instance):
        if not change_ticket_policy.user_has_permission_for_instance(
            self.request.user,
            "change",
            instance,
        ):
            return None

        if not self.edit_url_name:
            return None

        return reverse(
            self.edit_url_name,
            args=(quote(instance.pk),),
        )


# ============================================================
# ОТПРАВКА НА СОГЛАСОВАНИЕ
# ============================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def submit_for_approval_action(request, pk):
    """
    Отправляет собственный черновик исполнителя
    на согласование.
    """

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "change"),
        pk=pk,
    )

    try:
        submit_for_approval(
            ticket_id=ticket.pk,
            actor=request.user,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

    else:
        messages.success(
            request,
            "Карточка отправлена на согласование.",
        )

    return redirect(
        "change_tickets:inspect",
        pk=ticket.pk,
    )


# ============================================================
# СОГЛАСОВАНИЕ
# ============================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def approve_ticket_action(request, pk):
    """
    Согласовывает карточку руководителем.
    """

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "view"),
        pk=pk,
    )

    try:
        approve_ticket(
            ticket_id=ticket.pk,
            actor=request.user,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

    else:
        messages.success(
            request,
            "Изменение успешно согласовано.",
        )

    return redirect(
        "change_tickets:inspect",
        pk=ticket.pk,
    )


# ============================================================
# ОТКЛОНЕНИЕ
# ============================================================

@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def reject_ticket_action(request, pk):
    """
    Отклоняет карточку руководителем.

    Комментарий обязателен.
    После отклонения карточка возвращается в черновик.
    """

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "view"),
        pk=pk,
    )

    comment = request.POST.get("comment", "").strip()

    try:
        reject_ticket(
            ticket_id=ticket.pk,
            actor=request.user,
            comment=comment,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

        return redirect(
            "change_tickets:inspect",
            pk=ticket.pk,
        )

    messages.success(
        request,
        "Карточка отклонена и возвращена исполнителю.",
    )

    # Отклонённая карточка получает статус draft.
    # Руководитель больше не видит её в своём списке.
    return redirect("change_tickets:index")


# ============================================================
# УПРАВЛЕНИЕ ПЛАНОМ ИЗМЕНЕНИЙ
# ============================================================

def _get_plan_form_errors(form):
    return "; ".join(
        str(message)
        for field_errors in form.errors.values()
        for message in field_errors
    )


@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def plan_step_add_action(request, pk):
    """Добавление шага в собственный черновик."""

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "change"),
        pk=pk,
    )

    form = PlanStepForm(
        request.POST,
        ticket=ticket,
    )

    if not form.is_valid():
        messages.error(
            request,
            "Не удалось добавить шаг: "
            + _get_plan_form_errors(form),
        )
        return redirect(
            "change_tickets:inspect",
            pk=ticket.pk,
        )

    try:
        add_plan_step(
            ticket_id=ticket.pk,
            actor=request.user,
            **form.cleaned_data,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

    else:
        messages.success(
            request,
            "Шаг плана успешно добавлен.",
        )

    return redirect(
        "change_tickets:inspect",
        pk=ticket.pk,
    )


@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def plan_step_update_action(request, pk, step_id):
    """Редактирование шага собственного черновика."""

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "change"),
        pk=pk,
    )

    step = get_object_or_404(
        PlanStep,
        pk=step_id,
        ticket=ticket,
    )

    form = PlanStepForm(
        request.POST,
        ticket=ticket,
        step=step,
    )

    if not form.is_valid():
        messages.error(
            request,
            "Не удалось изменить шаг: "
            + _get_plan_form_errors(form),
        )
        return redirect(
            "change_tickets:inspect",
            pk=ticket.pk,
        )

    try:
        update_plan_step(
            ticket_id=ticket.pk,
            step_id=step.pk,
            actor=request.user,
            **form.cleaned_data,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

    else:
        messages.success(
            request,
            "Шаг плана успешно изменён.",
        )

    return redirect(
        "change_tickets:inspect",
        pk=ticket.pk,
    )


@require_POST
@permission_required(
    "wagtailadmin.access_admin",
    raise_exception=True,
)
def plan_step_delete_action(request, pk, step_id):
    """Удаление последнего шага плана."""

    ticket = get_object_or_404(
        _get_visible_tickets(request.user, "change"),
        pk=pk,
    )

    try:
        delete_plan_step(
            ticket_id=ticket.pk,
            step_id=step_id,
            actor=request.user,
        )

    except ValidationError as error:
        messages.error(
            request,
            _get_action_error_message(error),
        )

    else:
        messages.success(
            request,
            "Последний шаг плана удалён.",
        )

    return redirect(
        "change_tickets:inspect",
        pk=ticket.pk,
    )


# ============================================================
# VIEWSET КАРТОЧЕК ИЗМЕНЕНИЙ
# ============================================================

class ChangeTicketViewSet(ModelViewSet):
    """
    Административный интерфейс карточек изменений.

    Все стандартные представления используют единую
    ролевую политику доступа.
    """

    model = ChangeTicket

    permission_policy = change_ticket_policy

    # --------------------------------------------------------
    # Пользовательские представления
    # --------------------------------------------------------

    index_view_class = ChangeTicketIndexView
    add_view_class = ChangeTicketCreateView
    edit_view_class = ChangeTicketEditView
    inspect_view_class = ChangeTicketInspectView
    history_view_class = ChangeTicketHistoryView
    usage_view_class = ChangeTicketUsageView

    # --------------------------------------------------------
    # Форма
    # --------------------------------------------------------

    form_fields = [
        "deployment_target",
        "software_package",
        "source_version",
        "risks",
        "window_start_at",
        "window_end_at",
    ]

    def get_form_class(self, for_update=False):
        return ChangeTicketForm

    # --------------------------------------------------------
    # Меню
    # --------------------------------------------------------

    menu_label = "Карточки изменений"
    menu_icon = "doc-full-inverse"
    menu_item_class = ChangeTicketMenuItem
    menu_order = 320
    add_to_admin_menu = True

    # --------------------------------------------------------
    # Список
    # --------------------------------------------------------

    list_display = [
        "deployment_target",
        "software",
        "software_package",
        "source_version",
        "target_version",
        "status",
        "window_start_at",
        "window_end_at",
        "created_by",
        "created_at",
    ]

    list_filter = [
        "status",
        "deployment_target",
        "software",
        "software_package",
    ]

    search_fields = [
        "deployment_target__name",
        "software__name",
        "software_package__version",
        "source_version",
        "target_version",
        "risks",
    ]

    ordering = ["-created_at"]

    # --------------------------------------------------------
    # Дополнительные возможности
    # --------------------------------------------------------

    copy_view_enabled = False
    inspect_view_enabled = True

    # --------------------------------------------------------
    # Просмотр полей
    # --------------------------------------------------------

    inspect_view_fields = [
        "deployment_target",
        "software",
        "software_package",
        "created_by",
        "approved_by",
        "status",
        "source_version",
        "target_version",
        "risks",
        "window_start_at",
        "window_end_at",
        "rejection_comment",
        "created_at",
        "updated_at",
    ]

    # --------------------------------------------------------
    # Дополнительные маршруты рабочего процесса
    # --------------------------------------------------------

    def get_urlpatterns(self):
        urlpatterns = super().get_urlpatterns()

        conv = self.pk_path_converter

        urlpatterns.extend([
            path(
                f"submit/<{conv}:pk>/",
                submit_for_approval_action,
                name="submit_for_approval",
            ),
            path(
                f"approve/<{conv}:pk>/",
                approve_ticket_action,
                name="approve",
            ),
            path(
                f"reject/<{conv}:pk>/",
                reject_ticket_action,
                name="reject",
            ),
                        path(
                f"plan/<{conv}:pk>/add/",
                plan_step_add_action,
                name="plan_step_add",
            ),

            path(
                f"plan/<{conv}:pk>/steps/<{conv}:step_id>/update/",
                plan_step_update_action,
                name="plan_step_update",
            ),

            path(
                f"plan/<{conv}:pk>/steps/<{conv}:step_id>/delete/",
                plan_step_delete_action,
                name="plan_step_delete",
            ),
        ])

        return urlpatterns


# ============================================================
# РЕГИСТРАЦИЯ VIEWSET
# ============================================================

change_ticket_viewset = ChangeTicketViewSet(
    "change_tickets"
)
