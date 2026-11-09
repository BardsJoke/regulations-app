import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalogs.models import RollbackReason, TimeStampedModel
from changes.models import ChangeTicket, PlanStep, StepKind


# ============================================================
# ТИПЫ И СТАТУСЫ ОПЕРАЦИЙ
# ============================================================

class OperationType(models.TextChoices):
    UPDATE = "update", "Обновление"
    ROLLBACK = "rollback", "Откат"


class OperationStatus(models.TextChoices):
    RUNNING = "running", "Выполняется"
    COMPLETED = "completed", "Успешно завершена"
    FAILED = "failed", "Ошибка"
    CANCELLED = "cancelled", "Отменена"


class StepExecutionStatus(models.TextChoices):
    LOCKED = "locked", "Заблокирован"
    AVAILABLE = "available", "Доступен"
    IN_PROGRESS = "in_progress", "Выполняется"
    COMPLETED = "completed", "Выполнен"
    FAILED = "failed", "Ошибка"


# ============================================================
# OPERATION — ОПЕРАЦИЯ ОБНОВЛЕНИЯ ИЛИ ОТКАТА
# ============================================================

class Operation(TimeStampedModel):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    change_ticket = models.ForeignKey(
        ChangeTicket,
        on_delete=models.PROTECT,
        related_name="operations",
        verbose_name="Карточка изменения",
    )

    operation_type = models.CharField(
        max_length=10,
        choices=OperationType.choices,
        verbose_name="Тип операции",
    )

    sequence_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Порядковый номер операции",
    )

    attempt_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Номер попытки",
    )

    parent_operation = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rollback_operations",
        verbose_name="Исходная операция",
    )

    rollback_reason = models.ForeignKey(
        RollbackReason,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="operations",
        verbose_name="Причина отката",
    )

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="initiated_operations",
        verbose_name="Инициатор",
    )

    status = models.CharField(
        max_length=12,
        choices=OperationStatus.choices,
        default=OperationStatus.RUNNING,
        verbose_name="Статус операции",
    )

    started_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="Время начала",
    )

    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Время завершения",
    )

    result_summary = models.TextField(
        blank=True,
        verbose_name="Результат",
    )

    error_message = models.TextField(
        blank=True,
        verbose_name="Описание ошибки",
    )

    class Meta:
        verbose_name = "Операция"
        verbose_name_plural = "Операции"
        ordering = ["change_ticket", "sequence_number"]

        constraints = [
            models.UniqueConstraint(
                fields=["change_ticket", "sequence_number"],
                name="op_unique_ticket_sequence",
            ),

            models.UniqueConstraint(
                fields=[
                    "change_ticket",
                    "operation_type",
                    "attempt_number",
                ],
                name="op_unique_ticket_attempt",
            ),

            models.UniqueConstraint(
                fields=["change_ticket"],
                condition=Q(status=OperationStatus.RUNNING),
                name="op_one_running_per_ticket",
            ),

            models.CheckConstraint(
                condition=Q(sequence_number__gte=1),
                name="op_sequence_positive",
            ),

            models.CheckConstraint(
                condition=Q(attempt_number__gte=1),
                name="op_attempt_positive",
            ),

            models.CheckConstraint(
                condition=(
                    Q(
                        operation_type=OperationType.UPDATE,
                        parent_operation__isnull=True,
                        rollback_reason__isnull=True,
                    )
                    |
                    Q(
                        operation_type=OperationType.ROLLBACK,
                        parent_operation__isnull=False,
                        rollback_reason__isnull=False,
                    )
                ),
                name="op_type_parent_reason",
            ),

            models.CheckConstraint(
                condition=(
                    Q(
                        status=OperationStatus.RUNNING,
                        finished_at__isnull=True,
                    )
                    |
                    Q(
                        status__in=[
                            OperationStatus.COMPLETED,
                            OperationStatus.FAILED,
                            OperationStatus.CANCELLED,
                        ],
                        finished_at__isnull=False,
                    )
                ),
                name="op_finish_time_consistent",
            ),
        ]

        indexes = [
            models.Index(
                fields=["change_ticket", "created_at"],
                name="op_ticket_created_idx",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        if self.operation_type == OperationType.UPDATE:
            if self.parent_operation_id:
                errors["parent_operation"] = (
                    "Операция обновления не должна ссылаться "
                    "на исходную операцию отката."
                )

            if self.rollback_reason_id:
                errors["rollback_reason"] = (
                    "Для операции обновления причина отката не указывается."
                )

        elif self.operation_type == OperationType.ROLLBACK:
            if not self.parent_operation_id:
                errors["parent_operation"] = (
                    "Для отката необходимо указать исходную операцию."
                )
            else:
                parent = self.parent_operation

                if parent.operation_type != OperationType.UPDATE:
                    errors["parent_operation"] = (
                        "Откат должен ссылаться на операцию обновления."
                    )

                elif parent.change_ticket_id != self.change_ticket_id:
                    errors["parent_operation"] = (
                        "Исходная операция должна относиться "
                        "к той же карточке изменения."
                    )

            if not self.rollback_reason_id:
                errors["rollback_reason"] = (
                    "Необходимо указать причину отката."
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return (
            f"{self.get_operation_type_display()} "
            f"№{self.sequence_number}"
        )


# ============================================================
# STEP EXECUTION — ФАКТИЧЕСКИЙ ШАГ ОПЕРАЦИИ
# ============================================================

class StepExecution(TimeStampedModel):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    operation = models.ForeignKey(
        Operation,
        on_delete=models.PROTECT,
        related_name="steps",
        editable=False,
        verbose_name="Операция",
    )

    plan_step = models.ForeignKey(
        PlanStep,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="executions",
        editable=False,
        verbose_name="Исходный плановый шаг",
    )

    # Снимок плановых данных.
    # После создания записи эти поля не должны изменяться.

    step_order = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
        editable=False,
        verbose_name="Порядок шага",
    )

    step_kind = models.CharField(
        max_length=20,
        choices=StepKind.choices,
        editable=False,
        verbose_name="Тип шага",
    )

    name = models.CharField(
        max_length=200,
        editable=False,
        verbose_name="Название шага",
    )

    description = models.TextField(
        blank=True,
        editable=False,
        verbose_name="Описание шага",
    )

    planned_duration_min = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        editable=False,
        verbose_name="Плановая длительность, минут",
    )

    status = models.CharField(
        max_length=12,
        choices=StepExecutionStatus.choices,
        default=StepExecutionStatus.LOCKED,
        verbose_name="Статус выполнения",
    )

    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="started_step_executions",
        verbose_name="Кто начал выполнение",
    )

    finished_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="finished_step_executions",
        verbose_name="Кто завершил выполнение",
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Фактическое начало",
    )

    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Фактическое завершение",
    )

    result = models.TextField(
        blank=True,
        verbose_name="Результат выполнения",
    )

    error_message = models.TextField(
        blank=True,
        verbose_name="Описание ошибки",
    )

    # Поля снимка, запрещённые для изменения после создания.
    SNAPSHOT_FIELDS = (
        "operation_id",
        "plan_step_id",
        "step_order",
        "step_kind",
        "name",
        "description",
        "planned_duration_min",
    )

    class Meta:
        verbose_name = "Выполнение шага"
        verbose_name_plural = "Выполнения шагов"
        ordering = ["operation", "step_order"]

        constraints = [
            models.UniqueConstraint(
                fields=["operation", "step_order"],
                name="step_unique_operation_order",
            ),

            models.CheckConstraint(
                condition=Q(step_kind__in=StepKind.values),
                name="step_valid_kind",
            ),

            models.CheckConstraint(
                condition=Q(step_order__gte=1),
                name="step_order_positive",
            ),

            models.CheckConstraint(
                condition=Q(planned_duration_min__gte=1),
                name="step_duration_positive",
            ),

            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=[
                            StepExecutionStatus.LOCKED,
                            StepExecutionStatus.AVAILABLE,
                        ],
                        started_at__isnull=True,
                        finished_at__isnull=True,
                    )
                    |
                    Q(
                        status=StepExecutionStatus.IN_PROGRESS,
                        started_at__isnull=False,
                        finished_at__isnull=True,
                    )
                    |
                    Q(
                        status__in=[
                            StepExecutionStatus.COMPLETED,
                            StepExecutionStatus.FAILED,
                        ],
                        started_at__isnull=False,
                        finished_at__isnull=False,
                    )
                ),
                name="step_time_consistent",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        if not self.operation_id:
            return

        operation = self.operation

        if operation.operation_type == OperationType.UPDATE:

            if not self.plan_step_id:
                errors["plan_step"] = (
                    "Шаг операции обновления должен быть связан "
                    "с плановым шагом."
                )

            else:
                if (
                    self.plan_step.ticket_id
                    != operation.change_ticket_id
                ):
                    errors["plan_step"] = (
                        "Плановый шаг должен относиться "
                        "к той же карточке изменения."
                    )

                if self.step_order != self.plan_step.step_order:
                    errors["step_order"] = (
                        "Порядок фактического шага должен "
                        "соответствовать плановому."
                    )

            if (
                self.step_order == 1
                and self.step_kind != StepKind.BACKUP
            ):
                errors["step_kind"] = (
                    "Первый шаг операции обновления "
                    "должен быть резервным копированием."
                )

        elif operation.operation_type == OperationType.ROLLBACK:

            if self.plan_step_id:
                errors["plan_step"] = (
                    "Шаг отката не должен ссылаться "
                    "на плановый шаг обновления."
                )

        if (
            self.status in {
                StepExecutionStatus.IN_PROGRESS,
                StepExecutionStatus.COMPLETED,
                StepExecutionStatus.FAILED,
            }
            and not self.started_by_id
        ):
            errors["started_by"] = (
                "Необходимо указать пользователя, начавшего шаг."
            )

        if (
            self.status in {
                StepExecutionStatus.COMPLETED,
                StepExecutionStatus.FAILED,
            }
            and not self.finished_by_id
        ):
            errors["finished_by"] = (
                "Необходимо указать пользователя, завершившего шаг."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Проверяем, что снимок нельзя изменить после создания.
        if self.pk:
            previous = (
                type(self).objects
                .filter(pk=self.pk)
                .values(*self.SNAPSHOT_FIELDS)
                .first()
            )

            if previous:
                for field in self.SNAPSHOT_FIELDS:
                    if getattr(self, field) != previous[field]:
                        raise ValidationError(
                            "Нельзя изменять снимок фактического шага."
                        )

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.operation} — "
            f"шаг {self.step_order}: {self.name}"
        )


# ============================================================
# STEP TEMPLATE — ШАБЛОН ТЕХНИЧЕСКОГО ШАГА
# ============================================================

class StepTemplate(TimeStampedModel):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    step_kind = models.CharField(
        max_length=20,
        choices=StepKind.choices,
        verbose_name="Тип шага",
    )

    name = models.CharField(
        max_length=200,
        verbose_name="Название",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание",
    )

    default_duration_min = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name="Рекомендуемая длительность, минут",
    )

    sort_order = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="Порядок сортировки",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Активен",
    )

    class Meta:
        verbose_name = "Шаблон шага"
        verbose_name_plural = "Шаблоны шагов"
        ordering = ["sort_order", "name"]

        constraints = [
            models.UniqueConstraint(
                fields=["step_kind", "name"],
                name="template_unique_kind_name",
            ),

            models.CheckConstraint(
                condition=Q(default_duration_min__gte=1),
                name="template_duration_positive",
            ),

            models.CheckConstraint(
                condition=Q(sort_order__gte=1),
                name="template_sort_positive",
            ),
            models.CheckConstraint(
                condition=Q(step_kind__in=StepKind.values),
                name="template_valid_kind",
),
        ]

    def __str__(self):
        return self.name