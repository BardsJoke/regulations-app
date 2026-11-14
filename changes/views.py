
from django.core.exceptions import ValidationError
from django.shortcuts import redirect

from wagtail.admin import messages
from wagtail.admin.menu import MenuItem
from wagtail.admin.views import generic
from wagtail.admin.viewsets.model import ModelViewSet

from .forms import ChangeTicketForm
from .models import ChangeTicket
from .permissions import ChangeTicketPermissionPolicy
from .services import create_draft, update_draft


# Единая политика доступа ко всем карточкам.
change_ticket_policy = ChangeTicketPermissionPolicy(
    ChangeTicket
)


class ChangeTicketMenuItem(MenuItem):
    """
    Показывает раздел карточек пользователям
    с правом просмотра или создания.
    """

    def is_shown(self, request):
        return change_ticket_policy.user_has_any_permission(
            request.user,
            {"view", "add"},
        )


class ChangeTicketCreateView(generic.CreateView):
    """
    Создание карточки через сервис create_draft.
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


class ChangeTicketEditView(generic.EditView):
    """
    Редактирование черновика через сервис update_draft.
    """

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


class ChangeTicketViewSet(ModelViewSet):
    """
    Административный интерфейс карточек изменений.
    """

    model = ChangeTicket

    permission_policy = change_ticket_policy

    add_view_class = ChangeTicketCreateView
    edit_view_class = ChangeTicketEditView

    form_fields = [
        "deployment_target",
        "source_version",
        "target_version",
        "risks",
        "window_start_at",
        "window_end_at",
    ]

    def get_form_class(self, for_update=False):
        return ChangeTicketForm

    menu_label = "Карточки изменений"
    menu_icon = "doc-full-inverse"
    menu_item_class = ChangeTicketMenuItem
    menu_order = 320
    add_to_admin_menu = True

    list_display = [
        "deployment_target",
        "software",
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
    ]

    search_fields = [
        "deployment_target__name",
        "software__name",
        "source_version",
        "target_version",
        "risks",
    ]

    ordering = ["-created_at"]

    copy_view_enabled = False
    inspect_view_enabled = True

    inspect_view_fields = [
        "deployment_target",
        "software",
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


change_ticket_viewset = ChangeTicketViewSet(
    "change_tickets"
)
