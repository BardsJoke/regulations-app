
from pathlib import PureWindowsPath

from django import forms
from django.utils import timezone

from .models import (
    MuseumSoftware,
    SoftwarePackage,
    SoftwarePackageStatus,
)

from .package_validation import (
    PackageValidationError,
    inspect_uploaded_package,
)


class SoftwarePackageUploadForm(forms.ModelForm):
    """
    Форма регистрации нового ZIP-пакета.

    Версия, SHA-256, размер и манифест
    определяются автоматически из ZIP-архива.
    """

    class Meta:
        model = SoftwarePackage
        fields = [
            "software",
            "package_file",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.package_validation_result = None

        self.fields["software"].queryset = (
            MuseumSoftware.objects
            .filter(is_active=True)
            .order_by("name")
        )

        self.fields["software"].help_text = (
            "Выберите программный продукт, "
            "для которого предназначен пакет."
        )

        self.fields["package_file"].help_text = (
            "Загрузите ZIP-архив с manifest.json "
            "в корне архива."
        )

    def clean_package_file(self):
        uploaded_file = self.cleaned_data.get("package_file")

        if not uploaded_file:
            return uploaded_file

        try:
            self.package_validation_result = (
                inspect_uploaded_package(uploaded_file)
            )
        except PackageValidationError as exc:
            raise forms.ValidationError(str(exc)) from exc

        return uploaded_file

    def clean(self):
        cleaned_data = super().clean()

        software = cleaned_data.get("software")
        metadata = self.package_validation_result

        if software and metadata:
            duplicate = SoftwarePackage.objects.filter(
                software=software,
                version=metadata.version,
                status=SoftwarePackageStatus.VALID,
            ).exists()

            if duplicate:
                self.add_error(
                    "package_file",
                    (
                        f"Проверенный пакет версии "
                        f"{metadata.version} для этого ПО уже существует."
                    ),
                )

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        metadata = self.package_validation_result

        if metadata is None:
            raise ValueError(
                "Нельзя сохранить пакет без успешной проверки ZIP."
            )

        uploaded_file = self.cleaned_data["package_file"]

        original_filename = PureWindowsPath(
            uploaded_file.name.replace("/", "\\")
        ).name

        instance.original_filename = (
            original_filename[:255] or "package.zip"
        )

        instance.version = metadata.version
        instance.sha256 = metadata.sha256
        instance.size_bytes = metadata.size_bytes
        instance.manifest = metadata.manifest

        instance.status = SoftwarePackageStatus.VALID
        instance.verified_at = timezone.now()
        instance.validation_error = ""

        instance.clean()

        if commit:
            instance.save()
            self.save_m2m()

        return instance
