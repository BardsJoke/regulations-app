import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    email = models.EmailField(
        unique=True,
        verbose_name="Электронная почта",
    )

    def __str__(self):
        full_name = self.get_full_name().strip()
        return full_name or self.username