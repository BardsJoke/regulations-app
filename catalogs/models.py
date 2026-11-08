import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


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