# Права пользователей
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class ModuleRole(models.TextChoices):
    ADMINISTRATOR = "administrator", "Администратор"
    APPROVER = "approver", "Руководитель"
    EXECUTOR = "executor", "Исполнитель"


class RoleAssignment(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="regulations_role_assignments",
        verbose_name="Пользователь",
    )

    role = models.CharField(
        max_length=20,
        choices=ModuleRole.choices,
        verbose_name="Роль",
    )

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_regulations_roles",
        verbose_name="Назначил",
    )

    assigned_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата назначения",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата изменения",
    )

    class Meta:
        verbose_name = "Назначение роли"
        verbose_name_plural = "Назначения ролей"
        ordering = ["user__username"]

        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=ModuleRole.values),
                name="access_valid_module_role",
            ),
            models.UniqueConstraint(
                fields=["user", "role"],
                name="access_unique_user_role",
),
        ]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()}"