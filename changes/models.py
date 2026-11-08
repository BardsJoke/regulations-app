import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q

from catalogs.models import CardStatus, MuseumSoftware, TimeStampedModel


# Проверка формата SHA-256: ровно 64 шестнадцатеричных символа.
sha256_validator = RegexValidator(
    regex=r"^[0-9a-fA-F]{64}$",
    message="Контрольная сумма должна содержать 64 шестнадцатеричных символа.",
)


class HashStatus(models.TextChoices):
    NOT_CHECKED = "not_checked", "Не проверено"
    MATCH = "match", "Совпадает"
    MISMATCH = "mismatch", "Не совпадает"


class CompatibilityStatus(models.TextChoices):
    NOT_CHECKED = "not_checked", "Не проверено"
    COMPATIBLE = "compatible", "Совместимо"
    INCOMPATIBLE = "incompatible", "Несовместимо"


class ResourcesCheckStatus(models.TextChoices):
    NOT_CHECKED = "not_checked", "Не проверено"
    SUFFICIENT = "sufficient", "Ресурсов достаточно"
    INSUFFICIENT = "insufficient", "Недостаточно ресурсов"


class ChangeTicket(TimeStampedModel):
    """
    Карточка изменения программного обеспечения.

    Одновременно является плановым окном обновления.
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
        related_name="change_tickets",
        verbose_name="Программное обеспечение",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_change_tickets",
        verbose_name="Создал карточку",
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_change_tickets",
        verbose_name="Согласовал",
    )

    status = models.ForeignKey(
        CardStatus,
        on_delete=models.PROTECT,
        related_name="change_tickets",
        verbose_name="Статус",
    )

    source_version = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Исходная версия",
    )

    target_version = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Целевая версия",
    )

    risks = models.TextField(
        blank=True,
        verbose_name="Описание рисков",
    )

    package_file = models.FileField(
        upload_to="change-packages/%Y/%m/",
        blank=True,
        verbose_name="Пакет обновления",
    )

    expected_sha256 = models.CharField(
        max_length=64,
        blank=True,
        validators=[sha256_validator],
        verbose_name="Ожидаемая SHA-256",
    )

    actual_sha256 = models.CharField(
        max_length=64,
        blank=True,
        validators=[sha256_validator],
        verbose_name="Фактическая SHA-256",
    )

    hash_status = models.CharField(
        max_length=20,
        choices=HashStatus.choices,
        default=HashStatus.NOT_CHECKED,
        verbose_name="Результат проверки SHA-256",
    )

    compatibility_status = models.CharField(
        max_length=20,
        choices=CompatibilityStatus.choices,
        default=CompatibilityStatus.NOT_CHECKED,
        verbose_name="Проверка совместимости",
    )

    resources_check_status = models.CharField(
        max_length=20,
        choices=ResourcesCheckStatus.choices,
        default=ResourcesCheckStatus.NOT_CHECKED,
        verbose_name="Проверка ресурсов",
    )

    hash_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Дата проверки SHA-256",
    )

    window_start_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Начало планового окна",
    )

    window_end_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Окончание планового окна",
    )

    rejection_comment = models.TextField(
        blank=True,
        verbose_name="Комментарий при отклонении",
    )

    class Meta:
        verbose_name = "Карточка изменения"
        verbose_name_plural = "Карточки изменений"

        ordering = ["-created_at"]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(window_start_at__isnull=True)
                    | Q(window_end_at__isnull=True)
                    | Q(window_start_at__lt=F("window_end_at"))
                ),
                name="changes_valid_ticket_window",
            ),
        ]

        indexes = [
            models.Index(
                fields=["status", "window_start_at"],
                name="changes_status_window_idx",
            ),
        ]

    def clean(self):
        super().clean()

        if (
            self.window_start_at
            and self.window_end_at
            and self.window_start_at >= self.window_end_at
        ):
            raise ValidationError({
                "window_end_at": (
                    "Окончание планового окна должно быть позже его начала."
                )
            })

    def __str__(self):
        return (
            f"{self.software.name}: "
            f"{self.source_version or '—'} → "
            f"{self.target_version or '—'}"
        )
class ApprovalDecision(models.TextChoices):
    APPROVED = "approved", "Согласовано"
    REJECTED = "rejected", "Отклонено"
    RESCHEDULED = "rescheduled", "Перенесено"
    CANCELLED = "cancelled", "Отменено"


class ApprovalRecord(models.Model):
    """
    История решений руководителя по карточке изменения.

    Каждое решение записывается отдельной записью.
    Предыдущие решения не должны перезаписываться.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    ticket = models.ForeignKey(
        ChangeTicket,
        on_delete=models.PROTECT,
        related_name="approval_records",
        verbose_name="Карточка изменения",
    )

    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="change_approval_records",
        verbose_name="Руководитель",
    )

    decision = models.CharField(
        max_length=20,
        choices=ApprovalDecision.choices,
        verbose_name="Решение",
    )

    comment = models.TextField(
        blank=True,
        verbose_name="Комментарий",
    )

    old_start_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Прежнее начало окна",
    )

    old_end_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Прежнее окончание окна",
    )

    new_start_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Новое начало окна",
    )

    new_end_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Новое окончание окна",
    )

    decided_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата принятия решения",
    )

    class Meta:
        verbose_name = "Решение согласования"
        verbose_name_plural = "История согласований"
        ordering = ["-decided_at"]

    def __str__(self):
        return (
            f"{self.ticket} — "
            f"{self.get_decision_display()}"
        )
class StepKind(models.TextChoices):
    BACKUP = "backup", "Резервное копирование"
    UPDATE = "update", "Обновление"
    VERIFICATION = "verification", "Проверка"
    OTHER = "other", "Прочее"


class PlanStep(TimeStampedModel):
    """
    Плановый технический шаг карточки изменения.

    Существует до запуска операции и доступен
    руководителю для ознакомления.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    ticket = models.ForeignKey(
        ChangeTicket,
        on_delete=models.CASCADE,
        related_name="plan_steps",
        verbose_name="Карточка изменения",
    )

    step_order = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Порядок выполнения",
    )

    step_kind = models.CharField(
        max_length=20,
        choices=StepKind.choices,
        verbose_name="Тип шага",
    )

    name = models.CharField(
        max_length=200,
        verbose_name="Название шага",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание",
    )

    planned_duration_min = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Плановая длительность (минуты)",
    )

    class Meta:
        verbose_name = "Плановый шаг"
        verbose_name_plural = "Плановые шаги"
        ordering = ["step_order"]

        constraints = [
            models.UniqueConstraint(
                fields=["ticket", "step_order"],
                name="chg_ticket_step_order_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(step_order=1)
                    | Q(step_kind=StepKind.BACKUP)
                ),
                name="chg_first_step_backup",
            ),
        ]

    def clean(self):
        super().clean()

        if self.step_order == 1 and self.step_kind != StepKind.BACKUP:
            raise ValidationError({
                "step_kind": (
                    "Первым шагом должно быть резервное копирование."
                )
            })

    def __str__(self):
        return f"{self.step_order}. {self.name}"
class AttachmentKind(models.TextChoices):
    DOCUMENT = "document", "Документ"
    SCREENSHOT = "screenshot", "Скриншот"
    LOG = "log", "Журнал выполнения"
    OTHER = "other", "Прочее"


class ChangeAttachment(models.Model):
    """
    Файл, прикреплённый к карточке изменения.

    storage_key содержит ключ файла в хранилище,
    а не сам файл.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="Идентификатор",
    )

    ticket = models.ForeignKey(
        ChangeTicket,
        on_delete=models.PROTECT,
        related_name="attachments",
        verbose_name="Карточка изменения",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_change_attachments",
        verbose_name="Загрузил",
    )

    file_kind = models.CharField(
        max_length=20,
        choices=AttachmentKind.choices,
        verbose_name="Тип вложения",
    )

    original_name = models.CharField(
        max_length=255,
        verbose_name="Исходное имя файла",
    )

    storage_key = models.CharField(
        max_length=500,
        unique=True,
        verbose_name="Ключ файла в хранилище",
    )

    sha256 = models.CharField(
        max_length=64,
        validators=[sha256_validator],
        verbose_name="SHA-256 файла",
    )

    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата загрузки",
    )

    class Meta:
        verbose_name = "Вложение карточки"
        verbose_name_plural = "Вложения карточек"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_name