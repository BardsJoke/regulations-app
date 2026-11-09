import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalogs.models import TimeStampedModel
from operations.models import Operation, OperationType


# ============================================================
# СТАТУСЫ ЦЕЛОСТНОСТИ РЕЗЕРВНОЙ КОПИИ
# ============================================================

class BackupIntegrityStatus(models.TextChoices):
    NOT_CHECKED = "not_checked", "Не проверена"
    VALID = "valid", "Целостность подтверждена"
    INVALID = "invalid", "Нарушена целостность"
    ERROR = "error", "Ошибка проверки"


# ============================================================
# ПУТЬ ХРАНЕНИЯ ФАЙЛОВ
# ============================================================

def backup_upload_to(instance, filename):
    """
    Формирует безопасный путь хранения резервной копии.

    Имя пользовательского файла не используется.
    Каждая копия получает собственный UUID-каталог.
    """

    date_path = timezone.now().strftime("%Y/%m")

    return (
        f"backups/{date_path}/"
        f"{instance.pk}/backup.bin"
    )


# ============================================================
# BACKUP — РЕЗЕРВНАЯ КОПИЯ
# ============================================================

class Backup(TimeStampedModel):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    operation = models.ForeignKey(
        Operation,
        on_delete=models.PROTECT,
        related_name="backups",
        verbose_name="Операция",
    )

    file = models.FileField(
        upload_to=backup_upload_to,
        editable=False,
        verbose_name="Файл резервной копии",
    )

    file_sha256 = models.CharField(
        max_length=64,
        validators=[
            RegexValidator(
                regex=r"^[0-9a-f]{64}$",
                message="Необходимо указать SHA-256 в нижнем регистре.",
            )
        ],
        editable=False,
        verbose_name="SHA-256 файла",
    )

    size_bytes = models.PositiveBigIntegerField(
        editable=False,
        verbose_name="Размер файла, байт",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_backups",
        editable=False,
        verbose_name="Кто создал копию",
    )

    integrity_status = models.CharField(
        max_length=12,
        choices=BackupIntegrityStatus.choices,
        default=BackupIntegrityStatus.NOT_CHECKED,
        editable=False,
        verbose_name="Статус целостности",
    )

    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name="Дата проверки",
    )

    verification_error = models.TextField(
        blank=True,
        editable=False,
        verbose_name="Ошибка проверки",
    )

    class Meta:
        verbose_name = "Резервная копия"
        verbose_name_plural = "Резервные копии"
        ordering = ["-created_at"]

        constraints = [
            models.CheckConstraint(
                condition=Q(size_bytes__gte=0),
                name="backup_size_nonnegative",
            ),

            models.CheckConstraint(
                condition=(
                    Q(
                        integrity_status=BackupIntegrityStatus.NOT_CHECKED,
                        verified_at__isnull=True,
                        verification_error="",
                    )
                    |
                    Q(
                        integrity_status=BackupIntegrityStatus.VALID,
                        verified_at__isnull=False,
                        verification_error="",
                    )
                    |
                    (
                        Q(
                            integrity_status__in=[
                                BackupIntegrityStatus.INVALID,
                                BackupIntegrityStatus.ERROR,
                            ],
                            verified_at__isnull=False,
                        )
                        & ~Q(verification_error="")
                    )
                ),
                name="backup_integrity_state",
            ),
        ]

        indexes = [
            models.Index(
                fields=["operation", "created_at"],
                name="backup_op_created_idx",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        # Резервную копию создаём только в рамках UPDATE.
        if (
            self.operation_id
            and self.operation.operation_type != OperationType.UPDATE
        ):
            errors["operation"] = (
                "Резервная копия должна относиться "
                "к операции обновления."
            )

        # Проверяем согласованность статуса проверки.
        if self.integrity_status == BackupIntegrityStatus.NOT_CHECKED:
            if self.verified_at is not None:
                errors["verified_at"] = (
                    "Для непроверенной копии дата проверки "
                    "должна отсутствовать."
                )

            if self.verification_error:
                errors["verification_error"] = (
                    "Для непроверенной копии ошибка проверки "
                    "не указывается."
                )

        elif self.integrity_status == BackupIntegrityStatus.VALID:
            if self.verified_at is None:
                errors["verified_at"] = (
                    "Для подтверждённой копии укажите "
                    "дату проверки."
                )

            if self.verification_error:
                errors["verification_error"] = (
                    "Для успешно проверенной копии "
                    "ошибка должна отсутствовать."
                )

        elif self.integrity_status in {
            BackupIntegrityStatus.INVALID,
            BackupIntegrityStatus.ERROR,
        }:
            if self.verified_at is None:
                errors["verified_at"] = (
                    "Для завершённой проверки укажите дату."
                )

            if not self.verification_error:
                errors["verification_error"] = (
                    "Необходимо указать причину неудачной проверки."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Запрещает изменять исходные данные резервной копии
        после её создания.

        Результаты проверки целостности при этом изменять можно.
        """

        if self.pk:
            previous = (
                type(self).objects
                .filter(pk=self.pk)
                .values(
                    "operation_id",
                    "file",
                    "file_sha256",
                    "size_bytes",
                    "created_by_id",
                )
                .first()
            )

            if previous:
                current_values = {
                    "operation_id": self.operation_id,
                    "file": self.file.name if self.file else "",
                    "file_sha256": self.file_sha256,
                    "size_bytes": self.size_bytes,
                    "created_by_id": self.created_by_id,
                }

                for field, old_value in previous.items():
                    if current_values[field] != old_value:
                        raise ValidationError(
                            "Нельзя изменять исходные данные "
                            "созданной резервной копии."
                        )

        super().save(*args, **kwargs)

    @property
    def is_usable(self):
        """
        Можно ли использовать копию для восстановления.
        """

        return (
            self.integrity_status == BackupIntegrityStatus.VALID
            and self.verified_at is not None
        )

    def __str__(self):
        return (
            f"Резервная копия {self.operation} "
            f"({self.get_integrity_status_display()})"
        )