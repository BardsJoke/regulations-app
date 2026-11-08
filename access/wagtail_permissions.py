from django.contrib.auth import get_user_model
from wagtail.permission_policies import BasePermissionPolicy

from .models import ModuleRole


class ModuleAdminPermissionPolicy(BasePermissionPolicy):
    """
    Общая политика доступа к функциям модуля regulations.

    Управлять объектами могут только активные пользователи,
    которым назначена роль администратора модуля.
    """

    default_allowed_actions = frozenset({
        "view",
        "add",
        "change",
    })

    def __init__(self, model, allowed_actions=None):
        super().__init__(model)

        if allowed_actions is None:
            allowed_actions = self.default_allowed_actions

        self.allowed_actions = frozenset(allowed_actions)

    def users_with_any_permission(self, actions):
        if not self.allowed_actions.intersection(actions):
            return get_user_model().objects.none()

        return get_user_model().objects.filter(
            is_active=True,
            regulations_role__role=ModuleRole.ADMINISTRATOR,
        )


class RoleAssignmentPermissionPolicy(ModuleAdminPermissionPolicy):
    """
    Политика управления назначениями ролей.

    Оставлена отдельным именем для совместимости
    с уже созданным интерфейсом приложения access.
    """

    pass