from django.contrib.auth import get_user_model
from wagtail.permission_policies import BasePermissionPolicy

from .models import ModuleRole


class RoleAssignmentPermissionPolicy(BasePermissionPolicy):
    """
    Разрешает управлять назначениями ролей
    только активным пользователям с ролью администратора модуля.
    """

    allowed_actions = frozenset({
        "add",
        "change",
        "view",
    })

    def users_with_any_permission(self, actions):
        if not self.allowed_actions.intersection(actions):
            return get_user_model().objects.none()

        return get_user_model().objects.filter(
            is_active=True,
            regulations_role__role=ModuleRole.ADMINISTRATOR,
        )