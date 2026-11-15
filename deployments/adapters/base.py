
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError


class DeploymentAdapterError(ValidationError):
    """Ошибка при взаимодействии с целевой установкой."""


class DeploymentConfigurationError(DeploymentAdapterError):
    """Ошибка конфигурации адаптера."""


@dataclass(frozen=True)
class DeploymentInspection:
    """Результат проверки текущей установки."""

    target_id: str
    installed: bool
    current_version: str | None
    release_id: str | None
    release_path: str | None
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackagePreflight:
    """Результат предварительной проверки пакета обновления."""

    version: str
    sha256: str
    size_bytes: int
    file_count: int
    uncompressed_size_bytes: int


@dataclass(frozen=True)
class DeploymentActionResult:
    """Результат установки или восстановления."""

    target_id: str
    version: str
    release_id: str
    message: str


@dataclass(frozen=True)
class HealthCheckResult:
    """Результат проверки работоспособности установки."""

    ok: bool
    version: str | None
    message: str


class BaseDeploymentAdapter(ABC):
    """
    Базовый интерфейс адаптера развертывания.

    Каждый адаптер должен реализовать:
    - inspect;
    - preflight_package;
    - create_backup;
    - deploy;
    - health_check;
    - restore.
    """

    def __init__(self, target):
        self.target = target

    @abstractmethod
    def inspect(self) -> DeploymentInspection:
        """Проверяет текущую установку."""

    @abstractmethod
    def preflight_package(self, ticket) -> PackagePreflight:
        """Проверяет пакет обновления до установки."""

    @abstractmethod
    def create_backup(self, operation_id, actor):
        """Создаёт резервную копию."""

    @abstractmethod
    def deploy(self, ticket) -> DeploymentActionResult:
        """Устанавливает новую версию."""

    @abstractmethod
    def health_check(self, expected_version=None) -> HealthCheckResult:
        """Проверяет результат установки."""

    @abstractmethod
    def restore(self, backup_id, actor) -> DeploymentActionResult:
        """Восстанавливает установку из резервной копии."""
