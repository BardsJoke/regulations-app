
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction

from wagtail.admin.menu import MenuItem
from wagtail.admin.viewsets.model import (
    ModelViewSet,
    ModelViewSetGroup,
)
from wagtail.admin.views.generic.models import (
    CreateView as WagtailCreateView,
)

from access.wagtail_permissions import ModuleAdminPermissionPolicy

from .forms import SoftwarePackageUploadForm

from .models import (
    CardStatus,
    MuseumSoftware,
    RollbackReason,
    SoftwarePackage,
    SoftwarePackageStatus,
    SoftwareType,
)


# ==========================================================
# Политики доступа
# ==========================================================

software_type_policy = ModuleAdminPermissionPolicy(
    SoftwareType
)

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

# Пакеты можно просматривать и добавлять.
# Изменение и удаление зарегистрированных релизов запрещено.
software_package_policy = ModuleAdminPermissionPolicy(
    SoftwarePackage,
    allowed_actions={"view", "add"},
)


# ==========================================================
# Элементы меню
# ==========================================================

class CatalogMenuItem(MenuItem):
    """
    Показывает подразделы справочников
    только пользователям с доступом к каталогу.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and software_type_policy.user_has_permission(
                request.user,
                "view",
            )
        )


class SoftwarePackageMenuItem(MenuItem):
    """
    Отдельная проверка доступа к библиотеке пакетов.
    """

    def is_shown(self, request):
        return (
            request.user.is_authenticated
            and software_package_policy.user_has_permission(
                request.user,
                "view",
            )
        )


# ==========================================================
# Типы программного обеспечения
# ==========================================================

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


# ==========================================================
# Программное обеспечение
# ==========================================================

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


# ==========================================================
# Создание пакета: фиксация пользователя
# ==========================================================

class SoftwarePackageCreateView(WagtailCreateView):
    """
    Передаёт текущего пользователя в создаваемую запись.

    Если два администратора одновременно загружают
    одну версию, ограничение PostgreSQL не позволяет
    зарегистрировать два одинаковых проверенных пакета.
    """

    def form_valid(self, form):
        form.instance.uploaded_by = self.request.user

        try:
            with transaction.atomic():
                return super().form_valid(form)

        except IntegrityError:
            stored_name = form.instance.package_file.name

            if (
                stored_name
                and default_storage.exists(stored_name)
            ):
                default_storage.delete(stored_name)

            software = form.cleaned_data.get("software")
            metadata = form.package_validation_result

            if (
                software
                and metadata
                and SoftwarePackage.objects.filter(
                    software=software,
                    version=metadata.version,
                    status=SoftwarePackageStatus.VALID,
                ).exists()
            ):
                form.add_error(
                    "package_file",
                    (
                        "Пакет этой версии уже зарегистрирован "
                        "для выбранного программного обеспечения."
                    ),
                )
                return self.form_invalid(form)

            raise


# ==========================================================
# Библиотека пакетов программного обеспечения
# ==========================================================

class SoftwarePackageViewSet(ModelViewSet):
    model = SoftwarePackage
    permission_policy = software_package_policy

    add_view_class = SoftwarePackageCreateView

    menu_label = "Пакеты программного обеспечения"
    menu_icon = "snippet"
    menu_item_class = SoftwarePackageMenuItem

    # Используем свою форму загрузки.
    def get_form_class(self, for_update=False):
        return SoftwarePackageUploadForm

    form_fields = [
        "software",
        "package_file",
    ]

    list_display = [
        "software",
        "version",
        "original_filename",
        "size_bytes",
        "status",
        "uploaded_by",
        "uploaded_at",
    ]

    list_filter = [
        "software",
        "status",
    ]

    search_fields = [
        "version",
        "original_filename",
        "sha256",
        "software__name",
    ]

    ordering = [
        "-uploaded_at",
    ]

    inspect_view_enabled = True

    inspect_view_fields = [
        "software",
        "version",
        "original_filename",
        "sha256",
        "size_bytes",
        "status",
        "manifest",
        "uploaded_by",
        "uploaded_at",
        "verified_at",
        "validation_error",
    ]

    copy_view_enabled = False
    add_to_reference_index = False


# ==========================================================
# Статусы карточек
# ==========================================================

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


# ==========================================================
# Причины отката
# ==========================================================

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


# ==========================================================
# Общая группа каталога
# ==========================================================

class CatalogViewSetGroup(ModelViewSetGroup):
    """
    Объединяет справочники и библиотеку пакетов
    в одном разделе Wagtail Admin.
    """

    menu_label = "Справочники"
    menu_icon = "folder-open-inverse"
    menu_order = 310

    items = (
        SoftwareTypeViewSet("software_types"),
        MuseumSoftwareViewSet("museum_software"),
        SoftwarePackageViewSet("software_packages"),
        CardStatusViewSet("card_statuses"),
        RollbackReasonViewSet("rollback_reasons"),
    )
