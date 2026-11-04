import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):

    class Role(models.TextChoices):
        ADMINISTRATOR = "administrator", "Администратор"
        APPROVER = "approver", "Согласующий"
        EXECUTOR = "executor", "Исполнитель"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    email = models.EmailField(
        unique=True,
        verbose_name="Электронная почта",
    )

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.EXECUTOR,
        verbose_name="Роль пользователя",
    )

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"