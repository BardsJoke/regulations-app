
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# ============================================================
# QUERYSET — ЗАЩИТА ЖУРНАЛА ОТ ИЗМЕНЕНИЯ
# ============================================================

class AuditEventQuerySet(models.QuerySet):
    """
    Запрещает массовое изменение и удаление событий аудита.

    Создание новых событий разрешено.
    Изменение и удаление существующих событий запрещено.
    """

    def update(self, **kwargs):
        raise ValidationError(
            "Изменение записей журнала аудита запрещено."
        )

    def delete(self):
        raise ValidationError(
            "Удаление записей журнала аудита запрещено."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(
            "Массовое изменение записей аудита запрещено."
        )

    def bulk_create(self, objs, **kwargs):
        """
        Разрешает массовое создание только новых событий.
        Запрещает UPSERT и игнорирование конфликтов.
        """

        if kwargs.get("update_conflicts"):
            raise ValidationError(
                "Обновление существующих событий аудита запрещено."
            )

        if kwargs.get("ignore_conflicts"):
            raise ValidationError(
                "Нельзя игнорировать конфликты при регистрации аудита."
            )

        objects = list(objs)

        for event in objects:
            if not event._state.adding:
                raise ValidationError(
                    "Нельзя повторно добавлять существующую запись аудита."
                )

            event._capture_actor_name()
            event.full_clean()

        return super().bulk_create(objects, **kwargs)


# ============================================================
# AUDIT EVENT — СОБЫТИЕ ЖУРНАЛА АУДИТА
# ============================================================

class AuditEvent(models.Model):

    id = models.BigAutoField(
        primary_key=True,
        verbose_name="Идентификатор события",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_events",
        verbose_name="Пользователь",
    )

    actor_name_snapshot = models.CharField(
        max_length=255,
        editable=False,
        verbose_name="Имя пользователя на момент события",
    )

    action = models.CharField(
        max_length=100,
        verbose_name="Действие",
    )

    object_type = models.CharField(
        max_length=100,
        verbose_name="Тип объекта",
    )

    object_id = models.UUIDField(
        verbose_name="Идентификатор объекта",
    )

    occurred_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
        verbose_name="Время события",
    )

    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Подробности события",
    )

    ip_address = models.GenericIPAddressField(
        protocol="both",
        unpack_ipv4=True,
        null=True,
        blank=True,
        verbose_name="IP-адрес",
    )

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        verbose_name = "Событие аудита"
        verbose_name_plural = "Журнал аудита"

        ordering = [
            "-occurred_at",
            "-id",
        ]

        indexes = [
            models.Index(
                fields=["user", "-occurred_at"],
                name="audit_user_time_idx",
            ),

            models.Index(
                fields=["object_type", "object_id"],
                name="audit_object_idx",
            ),

            models.Index(
                fields=["action", "-occurred_at"],
                name="audit_action_time_idx",
            ),
        ]

    # ========================================================
    # ФОРМИРОВАНИЕ СНИМКА ИМЕНИ ПОЛЬЗОВАТЕЛЯ
    # ========================================================

    def _capture_actor_name(self):
        """
        Сохраняет имя пользователя на момент события.

        Сначала используется полное имя.
        Если оно не заполнено, используется username.
        """

        if not self.user_id:
            raise ValidationError({
                "user": "Необходимо указать пользователя события."
            })

        actor = self.user

        full_name = (
            actor.get_full_name().strip()
            if hasattr(actor, "get_full_name")
            else ""
        )

        username = (
            actor.get_username()
            if hasattr(actor, "get_username")
            else str(actor)
        )

        self.actor_name_snapshot = (
            full_name or username or str(actor)
        )[:255]

    # ========================================================
    # ВАЛИДАЦИЯ
    # ========================================================

    def clean(self):
        super().clean()

        errors = {}

        if not self.action or not self.action.strip():
            errors["action"] = (
                "Необходимо указать действие."
            )

        if not self.object_type or not self.object_type.strip():
            errors["object_type"] = (
                "Необходимо указать тип объекта."
            )

        if not isinstance(self.details, dict):
            errors["details"] = (
                "Подробности события должны иметь формат JSON-объекта."
            )

        if errors:
            raise ValidationError(errors)

    # ========================================================
    # СОХРАНЕНИЕ — APPEND-ONLY
    # ========================================================

    def save(self, *args, **kwargs):
        """
        Разрешает только создание новых событий.
        """

        if (
            not self._state.adding
            or (
                self.pk is not None
                and type(self)._base_manager.filter(pk=self.pk).exists()
            )
        ):
            raise ValidationError(
                "Изменение существующих событий аудита запрещено."
            )

        self._capture_actor_name()
        self.full_clean()

        super().save(*args, **kwargs)

    # ========================================================
    # УДАЛЕНИЕ — ЗАПРЕЩЕНО
    # ========================================================

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Удаление событий журнала аудита запрещено."
        )

    # ========================================================
    # RECORD — РЕГИСТРАЦИЯ СОБЫТИЯ
    # ========================================================

    @classmethod
    def record(
        cls,
        *,
        user,
        action,
        object_type=None,
        object_id=None,
        obj=None,
        details=None,
        ip_address=None,
    ):
        """
        Создаёт новую запись журнала аудита.

        Можно передать:
        - object_type и object_id отдельно;
        - либо obj, чтобы автоматически получить тип
          и UUID объекта.

        Пример:

        AuditEvent.record(
            user=user,
            action="operation.rollback.started",
            obj=operation,
            details={
                "backup_id": str(backup.pk),
            },
        )
        """

        if obj is not None:
            object_type = (
                object_type
                or f"{obj._meta.app_label}.{obj._meta.model_name}"
            )

            object_id = (
                object_id
                if object_id is not None
                else obj.pk
            )

        if not object_type or object_id is None:
            raise ValidationError(
                "Необходимо указать объект аудита "
                "или его тип и идентификатор."
            )

        event = cls(
            user=user,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details={} if details is None else details,
            ip_address=ip_address,
        )

        event.save()

        return event

    def __str__(self):
        return (
            f"{self.action} — "
            f"{self.actor_name_snapshot} — "
            f"{self.occurred_at:%d.%m.%Y %H:%M:%S}"
        )
