
from django import forms

from catalogs.models import (
    SoftwarePackage,
    SoftwarePackageStatus,
)
from deployments.models import (
    CONFIGURED_ADAPTER_TYPES,
    DeploymentEnvironment,
    DeploymentTarget,
)

from .models import ChangeTicket


class SoftwareRelationSelect(forms.Select):
    """
    Добавляет data-software-id к вариантам выбора.

    Используется JavaScript для фильтрации пакетов
    при изменении целевой установки.
    """

    def __init__(
        self,
        *args,
        software_id_by_value=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.software_id_by_value = software_id_by_value or {}

    def create_option(
        self,
        name,
        value,
        label,
        selected,
        index,
        subindex=None,
        attrs=None,
    ):
        option = super().create_option(
            name,
            value,
            label,
            selected,
            index,
            subindex=subindex,
            attrs=attrs,
        )

        if value not in (None, ""):
            software_id = self.software_id_by_value.get(str(value))

            if software_id:
                option["attrs"]["data-software-id"] = str(software_id)

        return option


class SoftwarePackageChoiceField(forms.ModelChoiceField):
    """
    Поле выбора проверенного пакета.
    """

    def label_from_instance(self, package):
        return f"{package.software.name} — версия {package.version}"


class ChangeTicketForm(forms.ModelForm):
    """
    Форма создания и редактирования черновика.

    Пользователь выбирает:
    1. Целевую установку.
    2. Проверенный ZIP-пакет для программного продукта этой установки.

    Целевая версия, SHA-256 и путь к файлу определяются сервисом
    автоматически по выбранному SoftwarePackage.
    """

    software_package = SoftwarePackageChoiceField(
        queryset=SoftwarePackage.objects.none(),
        required=True,
        label="Пакет обновления",
        help_text=(
            "Сначала выберите целевую установку. "
            "Будут доступны проверенные пакеты соответствующего ПО."
        ),
    )

    class Meta:
        model = ChangeTicket

        fields = [
            "deployment_target",
            "software_package",
            "source_version",
            "risks",
            "window_start_at",
            "window_end_at",
        ]

        widgets = {
            "source_version": forms.TextInput(
                attrs={
                    "placeholder": "Например, 1.0.0",
                }
            ),
            "risks": forms.Textarea(
                attrs={
                    "rows": 4,
                }
            ),
            "window_start_at": forms.DateTimeInput(
                attrs={
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
            "window_end_at": forms.DateTimeInput(
                attrs={
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # --------------------------------------------------
        # 1. Доступные целевые установки
        # --------------------------------------------------

        available_targets = (
            DeploymentTarget.objects
            .select_related("software")
            .filter(
                is_active=True,
                software__is_active=True,
                adapter_type__in=CONFIGURED_ADAPTER_TYPES,
            )
            .exclude(
                environment=DeploymentEnvironment.UNKNOWN,
            )
            .exclude(config_key="")
            .order_by(
                "software__name",
                "environment",
                "name",
            )
        )

        self.fields["deployment_target"].queryset = (
            available_targets
        )

        self.fields["deployment_target"].required = True

        self.fields["deployment_target"].label = (
            "Целевая установка"
        )

        self.fields["deployment_target"].help_text = (
            "Выберите конкретную установку программного обеспечения. "
            "Список пакетов будет отфильтрован автоматически."
        )

        target_software_map = {
            str(target.pk): str(target.software_id)
            for target in available_targets
        }

        target_widget_attrs = (
            self.fields["deployment_target"].widget.attrs.copy()
        )

        target_widget_attrs["data-change-ticket-target"] = "true"

        self.fields["deployment_target"].widget = (
            SoftwareRelationSelect(
                attrs=target_widget_attrs,
                software_id_by_value=target_software_map,
            )
        )

        # Передаём варианты выбора новому виджету.
        self.fields["deployment_target"].widget.choices = (
            self.fields["deployment_target"].choices
        )

        # --------------------------------------------------
        # 2. Только проверенные пакеты активного ПО
        # --------------------------------------------------

        available_packages = (
            SoftwarePackage.objects
            .select_related("software")
            .filter(
                status=SoftwarePackageStatus.VALID,
                software__is_active=True,
                verified_at__isnull=False,
                size_bytes__gt=0,
                sha256__regex=r"^[0-9a-fA-F]{64}$",
            )
            .exclude(package_file="")
            .order_by(
                "software__name",
                "version",
            )
        )

        self.fields["software_package"].queryset = (
            available_packages
        )

        package_software_map = {
            str(package.pk): str(package.software_id)
            for package in available_packages
        }

        package_widget_attrs = (
            self.fields["software_package"].widget.attrs.copy()
        )

        package_widget_attrs["data-change-ticket-package"] = "true"

        self.fields["software_package"].widget = (
            SoftwareRelationSelect(
                attrs=package_widget_attrs,
                software_id_by_value=package_software_map,
            )
        )

        # Передаём варианты выбора новому виджету.
        self.fields["software_package"].widget.choices = (
            self.fields["software_package"].choices
        )

        # --------------------------------------------------
        # 3. Подписи остальных полей
        # --------------------------------------------------

        self.fields["source_version"].label = (
            "Текущая установленная версия"
        )

        self.fields["source_version"].help_text = (
            "Укажите версию, которая сейчас установлена "
            "на выбранной целевой установке."
        )

        self.fields["risks"].label = (
            "Описание рисков"
        )

        self.fields["window_start_at"].label = (
            "Начало планового окна"
        )

        self.fields["window_end_at"].label = (
            "Окончание планового окна"
        )


    def clean(self):
        cleaned_data = super().clean()

        target = cleaned_data.get("deployment_target")
        package = cleaned_data.get("software_package")

        if target is None or package is None:
            return cleaned_data

        # --------------------------------------------------
        # Проверяем соответствие ПО
        # --------------------------------------------------

        if package.software_id != target.software_id:
            self.add_error(
                "software_package",
                (
                    "Выбранный пакет относится к другому "
                    "программному обеспечению."
                ),
            )
            return cleaned_data

        # --------------------------------------------------
        # Проверяем статус пакета
        # --------------------------------------------------

        if not package.is_usable:
            self.add_error(
                "software_package",
                (
                    "Выбранный пакет не прошёл проверку "
                    "или больше недоступен."
                ),
            )
            return cleaned_data

        # --------------------------------------------------
        # Проверяем наличие ZIP
        # --------------------------------------------------

        try:
            file_exists = (
                package.package_file.storage.exists(
                    package.package_file.name
                )
            )
        except Exception:
            self.add_error(
                "software_package",
                (
                    "Не удалось проверить наличие ZIP-файла "
                    "в хранилище."
                ),
            )
            return cleaned_data

        if not file_exists:
            self.add_error(
                "software_package",
                (
                    "ZIP-файл выбранного пакета отсутствует "
                    "в хранилище."
                ),
            )
            return cleaned_data

        # --------------------------------------------------
        # Подготавливаем экземпляр модели к проверке
        # --------------------------------------------------
        #
        # Важно: Django вызывает Model.clean() до сохранения
        # через create_draft().
        #
        # Программное обеспечение определяется целевой
        # установкой. Данные пакета берём из библиотеки ПО.
        #
        # Это не сохранение в БД, а подготовка временного
        # экземпляра для корректной проверки целостности.
        # --------------------------------------------------

        self.instance.deployment_target = target
        self.instance.software = target.software
        self.instance.software_package = package

        self.instance.target_version = package.version

        self.instance.expected_sha256 = (
            package.sha256.lower()
        )

        self.instance.package_file = (
            package.package_file.name
        )

        return cleaned_data

    class Media:
        js = (
            "changes/js/change-ticket-form.js",
        )
