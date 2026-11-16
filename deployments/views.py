
from wagtail.admin.menu import MenuItem
from wagtail.admin.viewsets.model import ModelViewSet

from access.wagtail_permissions import ModuleAdminPermissionPolicy

from .models import DeploymentTarget


# Управлять целевыми установками могут администраторы модуля.
# Удаление не разрешаем: установки могут использоваться карточками.
deployment_target_policy = ModuleAdminPermissionPolicy(
    DeploymentTarget,
    allowed_actions={"view", "add", "change"},
)


class DeploymentTargetMenuItem(MenuItem):
    """
    Показывает раздел целевых установок администраторам.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and deployment_target_policy.user_has_permission(
                request.user,
                "view",
            )
        )


class DeploymentTargetViewSet(ModelViewSet):
    """
    Управление целевыми установками музейного ПО.
    """

    model = DeploymentTarget
    permission_policy = deployment_target_policy

    menu_label = "Целевые установки"
    menu_icon = "site"
    menu_item_class = DeploymentTargetMenuItem
    menu_order = 330
    add_to_admin_menu = True

    form_fields = [
        "software",
        "name",
        "description",
        "environment",
        "adapter_type",
        "config_key",
        "is_active",
    ]

    list_display = [
        "name",
        "software",
        "environment",
        "adapter_type",
        "config_key",
        "is_active",
        "last_known_version",
        "last_inspected_at",
    ]

    list_filter = [
        "software",
        "environment",
        "adapter_type",
        "is_active",
    ]

    search_fields = [
        "name",
        "description",
        "software__name",
        "config_key",
    ]

    ordering = [
        "software__name",
        "environment",
        "name",
    ]

    inspect_view_enabled = True

    inspect_view_fields = [
        "software",
        "name",
        "description",
        "environment",
        "adapter_type",
        "config_key",
        "last_known_version",
        "last_inspected_at",
        "is_active",
        "created_at",
        "updated_at",
    ]

    copy_view_enabled = False


deployment_target_viewset = DeploymentTargetViewSet(
    "deployment_targets"
)
