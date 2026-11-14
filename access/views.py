
from django.db import transaction

from wagtail.admin.menu import MenuItem
from wagtail.admin.views import generic
from wagtail.admin.viewsets.model import ModelViewSet

from .models import RoleAssignment
from .wagtail_permissions import RoleAssignmentPermissionPolicy
from .forms import RoleAssignmentForm
from .services import (
    RoleAssignmentWorkflowError,
    record_role_assigned,
    record_role_assignment_change,
    record_role_revoked,
)


# Единая политика доступа к назначениям ролей.
role_assignment_policy = RoleAssignmentPermissionPolicy(
    RoleAssignment
)


class RoleAssignmentMenuItem(MenuItem):
    """
    Показывает пункт меню только администраторам модуля.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and role_assignment_policy.user_has_permission(
                request.user,
                "view",
            )
        )


class RoleAssignmentCreateView(generic.CreateView):
    """
    Создаёт назначение роли и фиксирует его в аудите.
    """

    def save_instance(self):
        self.form.instance.assigned_by = self.request.user

        assignment = super().save_instance()

        record_role_assigned(
            assignment=assignment,
            actor=self.request.user,
        )

        return assignment


class RoleAssignmentEditView(generic.EditView):
    """
    Редактирует назначение роли и фиксирует изменения в аудите.
    """

    def save_instance(self):
        # Wagtail вызывает этот метод внутри transaction.atomic().
        # Получаем исходное состояние непосредственно из БД.
        old_assignment = (
            RoleAssignment.objects
            .select_for_update()
            .get(pk=self.form.instance.pk)
        )

        old_user_id = old_assignment.user_id
        old_role = old_assignment.role

        assignment = super().save_instance()

        record_role_assignment_change(
            assignment=assignment,
            actor=self.request.user,
            old_user_id=old_user_id,
            old_role=old_role,
        )

        return assignment

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except RoleAssignmentWorkflowError as error:
            form.add_error("role", error)
            return self.form_invalid(form)


class RoleAssignmentDeleteView(generic.DeleteView):
    """
    Удаляет назначение роли и фиксирует отзыв в аудите.
    """

    @transaction.atomic
    def delete_action(self):
        # Событие создаётся перед удалением.
        # Если удаление завершится ошибкой, транзакция
        # отменит и запись аудита.
        record_role_revoked(
            assignment=self.object,
            actor=self.request.user,
        )

        # Сохраняем стандартное поведение Wagtail.
        return super().delete_action()

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except RoleAssignmentWorkflowError as error:
            form.add_error(None, error)
            return self.form_invalid(form)


class RoleAssignmentViewSet(ModelViewSet):
    """
    Интерфейс управления назначениями ролей.
    """

    model = RoleAssignment

    permission_policy = role_assignment_policy

    add_view_class = RoleAssignmentCreateView
    edit_view_class = RoleAssignmentEditView
    delete_view_class = RoleAssignmentDeleteView

    # Поля формы создания и редактирования.
    form_fields = [
        "user",
        "role",
    ]

    # Колонки списка назначений.
    list_display = [
        "__str__",
        "assigned_by",
        "assigned_at",
    ]

    ordering = ["user__username"]

    # Настройки пункта меню.
    menu_label = "Назначения ролей"
    menu_item_class = RoleAssignmentMenuItem
    menu_icon = "user"
    menu_order = 300
    add_to_admin_menu = True

    copy_view_enabled = False
    inspect_view_enabled = False

    def get_form_class(self, for_update=False):
        return RoleAssignmentForm
