from wagtail.admin.menu import MenuItem
from wagtail.admin.viewsets.model import (
    ModelViewSet,
    ModelViewSetGroup,
)

from access.wagtail_permissions import ModuleAdminPermissionPolicy

from .models import (
    CardStatus,
    MuseumSoftware,
    RollbackReason,
    SoftwareType,
)


# Отдельная политика для каждой модели.
software_type_policy = ModuleAdminPermissionPolicy(SoftwareType)

museum_software_policy = ModuleAdminPermissionPolicy(
    MuseumSoftware
)

card_status_policy = ModuleAdminPermissionPolicy(
    CardStatus,
    allowed_actions={"view", "change"},
)

rollback_reason_policy = ModuleAdminPermissionPolicy(
    RollbackReason
)


class CatalogMenuItem(MenuItem):
    """
    Показывает подразделы справочников
    только администраторам модуля.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and software_type_policy.user_has_permission(
                request.user,
                "view",
            )
        )


class SoftwareTypeViewSet(ModelViewSet):
    model = SoftwareType
    permission_policy = software_type_policy

    menu_label = "Типы ПО"
    menu_icon = "snippet"
    menu_item_class = CatalogMenuItem

    form_fields = [
        "name",
        "is_active",
    ]

    list_display = [
        "name",
        "is_active",
        "updated_at",
    ]

    search_fields = ["name"]

    copy_view_enabled = False
    inspect_view_enabled = False


class MuseumSoftwareViewSet(ModelViewSet):
    model = MuseumSoftware
    permission_policy = museum_software_policy

    menu_label = "Программное обеспечение"
    menu_icon = "snippet"
    menu_item_class = CatalogMenuItem

    form_fields = [
        "software_type",
        "name",
        "description",
        "current_version",
        "is_active",
    ]

    list_display = [
        "name",
        "software_type",
        "current_version",
        "is_active",
        "updated_at",
    ]

    list_filter = [
        "software_type",
        "is_active",
    ]

    search_fields = [
        "name",
        "description",
        "current_version",
    ]

    copy_view_enabled = False
    inspect_view_enabled = False


class CardStatusViewSet(ModelViewSet):
    model = CardStatus
    permission_policy = card_status_policy

    menu_label = "Статусы карточек"
    menu_icon = "snippet"
    menu_item_class = CatalogMenuItem

    form_fields = [
        "name",
        "sort_order",
    ]

    list_display = [
        "code",
        "name",
        "sort_order",
        "updated_at",
    ]

    ordering = ["sort_order"]
    search_fields = ["code", "name"]

    copy_view_enabled = False
    inspect_view_enabled = False


class RollbackReasonViewSet(ModelViewSet):
    model = RollbackReason
    permission_policy = rollback_reason_policy

    menu_label = "Причины отката"
    menu_icon = "snippet"
    menu_item_class = CatalogMenuItem

    form_fields = [
        "name",
        "is_active",
    ]

    list_display = [
        "name",
        "is_active",
        "updated_at",
    ]

    search_fields = ["name"]

    copy_view_enabled = False
    inspect_view_enabled = False


class CatalogViewSetGroup(ModelViewSetGroup):
    """
    Объединяет четыре справочника
    в одном разделе Wagtail.
    """

    menu_label = "Справочники"
    menu_icon = "folder-open-inverse"
    menu_order = 310

    items = (
        SoftwareTypeViewSet("software_types"),
        MuseumSoftwareViewSet("museum_software"),
        CardStatusViewSet("card_statuses"),
        RollbackReasonViewSet("rollback_reasons"),
    )