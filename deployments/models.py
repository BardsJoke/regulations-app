
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from catalogs.models import MuseumSoftware


class DeploymentEnvironment(models.TextChoices):
    """
    Среды, в которых может быть установлено программное обеспечение.
    """

    DEVELOPMENT = "development", "Разработка"
    TEST = "test", "Тестовая"
    STAGING = "staging", "Предварительная"
    PRODUCTION = "production", "Рабочая"

    UNKNOWN = "unknown", "Не определена"


class DeploymentAdapterType(models.TextChoices):
    """
    Поддерживаемые способы взаимодействия с целевой установкой.

    UNCONFIGURED используется для ещё не настроенных установок
    и исторических записей, перенесённых из старой модели.
    """

    UNCONFIGURED = "unconfigured", "Не настроен"
    LOCAL_FILESYSTEM = "local_filesystem", "Локальная файловая система"
    DOCKER = "docker", "Docker"
    SSH = "ssh", "SSH"
    WINDOWS_AGENT = "windows_agent", "Агент Windows"


CONFIGURED_ADAPTER_TYPES = (
    DeploymentAdapterType.LOCAL_FILESYSTEM,
    DeploymentAdapterType.DOCKER,
    DeploymentAdapterType.SSH,
    DeploymentAdapterType.WINDOWS_AGENT,
)


class DeploymentTarget(models.Model):
    """
    Конкретная установка программного обеспечения.

    MuseumSoftware описывает сам программный продукт.
    DeploymentTarget описывает конкретный экземпляр этого продукта,
    установленный в определённой среде.

    Физические пути, пароли и другие секреты здесь не хранятся.
    config_key используется только для поиска серверной конфигурации.
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
        related_name="deployment_targets",
        verbose_name="Программное обеспечение",
    )

    name = models.CharField(
        max_length=255,
        verbose_name="Название установки",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание",
    )

    environment = models.CharField(
        max_length=20,
        choices=DeploymentEnvironment.choices,
        verbose_name="Среда установки",
    )

    adapter_type = models.CharField(
        max_length=30,
        choices=DeploymentAdapterType.choices,
        default=DeploymentAdapterType.UNCONFIGURED,
        verbose_name="Тип адаптера",
    )

    config_key = models.CharField(
        max_length=100,
        blank=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
                message=(
                    "Ключ конфигурации может содержать только "
                    "латинские буквы, цифры, точки, дефисы и "
                    "знаки подчёркивания. Первый символ должен "
                    "быть буквой или цифрой."
                ),
            ),
        ],
        help_text=(
            "Ключ серверной конфигурации. Не является путём "
            "к файлам или строкой подключения."
        ),
        verbose_name="Ключ конфигурации",
    )

    last_known_version = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Последняя известная версия",
        help_text=(
            "Последняя версия, подтверждённая проверкой "
            "целевой установки."
        ),
    )

    last_inspected_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Последняя проверка",
    )

    is_active = models.BooleanField(
        default=False,
        verbose_name="Активна",
        help_text=(
            "Активная установка может использоваться "
            "для выполнения операций после проверки конфигурации."
        ),
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата изменения",
    )

    class Meta:
        verbose_name = "Целевая установка"
        verbose_name_plural = "Целевые установки"
        ordering = ["name"]

        constraints = [
            models.CheckConstraint(
                condition=Q(
                    environment__in=DeploymentEnvironment.values
                ),
                name="deployments_valid_environment",
            ),
            models.CheckConstraint(
                condition=Q(
                    adapter_type__in=DeploymentAdapterType.values
                ),
                name="deployments_valid_adapter",
            ),
            models.UniqueConstraint(
                fields=["software", "environment", "name"],
                name="deployments_unique_target_name",
            ),
            models.UniqueConstraint(
                fields=["config_key"],
                condition=~Q(config_key=""),
                name="deployments_unique_config_key",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        adapter_type=DeploymentAdapterType.UNCONFIGURED,
                        config_key="",
                    )
                    | (
                        Q(adapter_type__in=CONFIGURED_ADAPTER_TYPES)
                        & ~Q(config_key="")
                    )
                ),
                name="deployments_adapter_config_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_active=False)
                    | (
                        Q(adapter_type__in=CONFIGURED_ADAPTER_TYPES)
                        & ~Q(config_key="")
                        & ~Q(environment=DeploymentEnvironment.UNKNOWN)
                    )
                ),
                name="deployments_active_requires_config",
            ),
        ]

        indexes = [
            models.Index(
                fields=["environment", "is_active"],
                name="deployments_env_active_idx",
            ),
        ]

    def clean(self):
        """
        Дополнительная проверка согласованности настроек установки.
        """

        errors = {}

        if self.adapter_type == DeploymentAdapterType.UNCONFIGURED:
            if self.config_key:
                errors["config_key"] = (
                    "У ненастроенной установки не должно быть "
                    "ключа конфигурации."
                )

        elif self.adapter_type in CONFIGURED_ADAPTER_TYPES:
            if not self.config_key:
                errors["config_key"] = (
                    "Для настроенного адаптера необходимо указать "
                    "ключ серверной конфигурации."
                )

        if self.is_active:
            if self.adapter_type == DeploymentAdapterType.UNCONFIGURED:
                errors["adapter_type"] = (
                    "Нельзя активировать установку без настроенного адаптера."
                )

            if not self.config_key:
                errors["config_key"] = (
                    "Нельзя активировать установку без ключа конфигурации."
                )

            if self.environment == DeploymentEnvironment.UNKNOWN:
                errors["environment"] = (
                    "Нельзя активировать установку с неопределённой средой."
                )

        if errors:
            raise ValidationError(errors)

    @property
    def is_configured(self):
        """
        Показывает, достаточно ли настроек для взаимодействия
        с целевой установкой.
        """

        return (
            self.adapter_type in CONFIGURED_ADAPTER_TYPES
            and bool(self.config_key)
            and self.environment != DeploymentEnvironment.UNKNOWN
        )

    @property
    def can_execute(self):
        """
        Показывает, разрешено ли использовать установку
        для выполнения операций.
        """

        return self.is_active and self.is_configured

    def __str__(self):
        return (
            f"{self.software.name} — {self.name} "
            f"({self.get_environment_display()})"
        )
