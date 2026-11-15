import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from django.conf import settings
from django.core.validators import FileExtensionValidator

class TimeStampedModel(models.Model):
    """
    Абстрактная модель с датами создания и изменения.
    Используется как базовый класс для справочников.
    """

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата изменения",
    )

    class Meta:
        abstract = True


class CardStatusCode(models.TextChoices):
    """
    Фиксированные системные коды статусов карточек.
    """

    DRAFT = "draft", "Черновик"
    PENDING = "pending", "На согласовании"
    APPROVED = "approved", "Согласовано"
    IN_PROGRESS = "in_progress", "В работе"
    COMPLETED = "completed", "Успешно завершено"
    ROLLED_BACK = "rolled_back", "Откат изменений"
    CANCELLED = "cancelled", "Отменено"


class SoftwareType(TimeStampedModel):
    """
    Тип музейного программного обеспечения.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    name = models.CharField(
        max_length=200,
        unique=True,
        verbose_name="Название типа ПО",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Активен",
    )

    class Meta:
        verbose_name = "Тип программного обеспечения"
        verbose_name_plural = "Типы программного обеспечения"
        ordering = ["name"]

    def __str__(self):
        return self.name


class MuseumSoftware(TimeStampedModel):
    """
    Программное обеспечение, используемое музеем.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    software_type = models.ForeignKey(
        SoftwareType,
        on_delete=models.PROTECT,
        related_name="museum_software",
        verbose_name="Тип программного обеспечения",
    )

    name = models.CharField(
        max_length=255,
        verbose_name="Название ПО",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание",
    )

    current_version = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Текущая версия",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Активно",
    )

    class Meta:
        verbose_name = "Программное обеспечение музея"
        verbose_name_plural = "Программное обеспечение музея"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CardStatus(TimeStampedModel):
    """
    Справочник системных статусов карточек изменений.

    Системный код фиксирован.
    Название и порядок отображения могут изменяться.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    code = models.CharField(
        max_length=20,
        choices=CardStatusCode.choices,
        unique=True,
        editable=False,
        verbose_name="Системный код",
    )

    name = models.CharField(
        max_length=100,
        verbose_name="Название статуса",
    )

    sort_order = models.PositiveSmallIntegerField(
        verbose_name="Порядок отображения",
    )

    is_active = models.BooleanField(
        default=True,
        editable=False,
        verbose_name="Активен",
    )

    class Meta:
        verbose_name = "Статус карточки"
        verbose_name_plural = "Статусы карточек"
        ordering = ["sort_order"]

        constraints = [
            models.CheckConstraint(
                condition=Q(code__in=CardStatusCode.values),
                name="catalogs_valid_card_status_code",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old_code = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list("code", flat=True)
                .first()
            )

            if old_code is not None and old_code != self.code:
                raise ValidationError(
                    {"code": "Системный код статуса нельзя изменить."}
                )

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Системные статусы нельзя удалять."
        )

    def __str__(self):
        return self.name


class RollbackReason(TimeStampedModel):
    """
    Справочник причин отката обновления.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    name = models.CharField(
        max_length=200,
        unique=True,
        verbose_name="Причина отката",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Активна",
    )

    class Meta:
        verbose_name = "Причина отката"
        verbose_name_plural = "Причины отката"
        ordering = ["name"]

    def __str__(self):
        return self.name

class SoftwarePackageStatus(models.TextChoices):
    PENDING = "pending", "Ожидает проверки"
    VALID = "valid", "Проверен"
    INVALID = "invalid", "Проверка не пройдена"


def software_package_upload_to(instance, filename):
    """
    Формирует внутренний путь хранения ZIP-файла.

    Исходное имя файла не используется как путь хранения.
    Каждый пакет получает собственный каталог.
    """
    return (
        f"software_packages/"
        f"{instance.software_id}/"
        f"{instance.pk}/"
        f"package.zip"
    )


class SoftwarePackage(TimeStampedModel):
    """
    Загруженный пакет конкретной версии музейного ПО.

    Метаданные хранятся в PostgreSQL.
    Сам ZIP-файл хранится в MEDIA_ROOT.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    software = models.ForeignKey(
        MuseumSoftware,
        on_delete=models.PROTECT,
        related_name="packages",
        verbose_name="Программное обеспечение",
    )

    version = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Версия пакета",
    )

    package_file = models.FileField(
        upload_to=software_package_upload_to,
        max_length=180,
        validators=[
            FileExtensionValidator(
                allowed_extensions=["zip"],
            ),
        ],
        verbose_name="ZIP-пакет",
    )

    original_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Исходное имя файла",
    )

    sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="SHA-256",
    )

    size_bytes = models.PositiveBigIntegerField(
        default=0,
        verbose_name="Размер файла в байтах",
    )

    manifest = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Манифест пакета",
    )

    status = models.CharField(
        max_length=20,
        choices=SoftwarePackageStatus.choices,
        default=SoftwarePackageStatus.PENDING,
        verbose_name="Статус проверки",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_software_packages",
        null=True,
        blank=True,
        verbose_name="Загрузил",
    )

    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата загрузки",
    )

    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Дата проверки",
    )

    validation_error = models.TextField(
        blank=True,
        verbose_name="Ошибка проверки",
    )

    class Meta:
        verbose_name = "Пакет программного обеспечения"
        verbose_name_plural = "Пакеты программного обеспечения"

        ordering = [
            "software__name",
            "version",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=["software", "version"],
                condition=models.Q(status="valid"),
                name="catalogs_unique_valid_package_version",
            ),
        ]

        indexes = [
            models.Index(
                fields=["software", "status"],
                name="cat_pkg_software_status_idx",
            ),
            models.Index(
                fields=["uploaded_at"],
                name="cat_pkg_uploaded_at_idx",
            ),
        ]

    def clean(self):
        super().clean()

        if self.status == SoftwarePackageStatus.VALID:
            errors = {}

            if not self.version.strip():
                errors["version"] = (
                    "У проверенного пакета должна быть указана версия."
                )

            if (
                len(self.sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in self.sha256.lower()
                )
            ):
                errors["sha256"] = (
                    "У проверенного пакета должен быть корректный SHA-256."
                )

            if self.size_bytes <= 0:
                errors["size_bytes"] = (
                    "Размер проверенного пакета должен быть больше нуля."
                )

            if not self.verified_at:
                errors["verified_at"] = (
                    "Необходимо указать дату проверки."
                )

            if not self.package_file:
                errors["package_file"] = (
                    "У проверенного пакета должен быть ZIP-файл."
                )

            if self.validation_error:
                errors["validation_error"] = (
                    "У проверенного пакета не должно быть ошибки проверки."
                )

            if errors:
                raise ValidationError(errors)

        elif (
            self.status == SoftwarePackageStatus.INVALID
            and not self.validation_error.strip()
        ):
            raise ValidationError({
                "validation_error": (
                    "Укажите причину, по которой пакет не прошёл проверку."
                )
            })

    @property
    def is_usable(self):
        return (
            self.status == SoftwarePackageStatus.VALID
            and bool(self.package_file)
            and len(self.sha256) == 64
            and self.verified_at is not None
        )

    def __str__(self):
        version = self.version or "версия не определена"
        return f"{self.software.name} — {version}"
