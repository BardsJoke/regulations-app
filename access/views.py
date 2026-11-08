from wagtail.admin.menu import MenuItem
from wagtail.admin.views import generic
from wagtail.admin.viewsets.model import ModelViewSet

from .models import RoleAssignment
from .wagtail_permissions import RoleAssignmentPermissionPolicy

from .forms import RoleAssignmentForm


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
    Автоматически записывает пользователя,
    который назначил роль.
    """

    def save_instance(self):
        self.form.instance.assigned_by = self.request.user

        return super().save_instance()


class RoleAssignmentViewSet(ModelViewSet):
    """
    Интерфейс управления назначениями ролей.
    """

    model = RoleAssignment

    permission_policy = role_assignment_policy

    add_view_class = RoleAssignmentCreateView

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