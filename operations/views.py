from wagtail.admin.menu import MenuItem
from wagtail.admin.viewsets.model import ModelViewSet

from access.wagtail_permissions import ModuleAdminPermissionPolicy

from .models import StepTemplate


# ==========================================================
# Политика доступа
# ==========================================================

step_template_policy = ModuleAdminPermissionPolicy(
    StepTemplate,
)


# ==========================================================
# Элемент меню
# ==========================================================

class StepTemplateMenuItem(MenuItem):
    """
    Показывает раздел шаблонов операций
    только администраторам модуля.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and step_template_policy.user_has_permission(
                request.user,
                "view",
            )
        )


# ==========================================================
# Шаблоны операций
# ==========================================================

class StepTemplateViewSet(ModelViewSet):
    """
    Административное управление шаблонами шагов
    обновления и отката.
    """

    model = StepTemplate

    permission_policy = step_template_policy

    menu_label = "Шаблоны операций"
    menu_icon = "snippet"
    menu_item_class = StepTemplateMenuItem
    menu_order = 315

    add_to_admin_menu = True

    form_fields = [
        "operation_type",
        "step_kind",
        "name",
        "description",
        "default_duration_min",
        "sort_order",
        "is_active",
    ]

    list_display = [
        "operation_type",
        "step_kind",
        "name",
        "default_duration_min",
        "sort_order",
        "is_active",
        "updated_at",
    ]

    list_filter = [
        "operation_type",
        "step_kind",
        "is_active",
    ]

    search_fields = [
        "name",
        "description",
    ]

    ordering = [
        "operation_type",
        "sort_order",
        "name",
    ]

    inspect_view_enabled = True

    inspect_view_fields = [
        "operation_type",
        "step_kind",
        "name",
        "description",
        "default_duration_min",
        "sort_order",
        "is_active",
        "created_at",
        "updated_at",
    ]

    copy_view_enabled = False
    add_to_reference_index = False