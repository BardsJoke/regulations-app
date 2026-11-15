
from deployments.models import (
    DeploymentAdapterType,
)

from .base import (
    DeploymentAdapterError,
    DeploymentConfigurationError,
)


def get_deployment_adapter(target):
    """
    Возвращает адаптер для указанной целевой установки.

    Физические настройки разрешаются на сервере по config_key.
    """

    if not target.is_configured:
        raise DeploymentConfigurationError(
            "Целевая установка не настроена."
        )

    if target.adapter_type == DeploymentAdapterType.LOCAL_FILESYSTEM:
        from .local_filesystem import LocalFilesystemAdapter

        return LocalFilesystemAdapter(target)

    if target.adapter_type == DeploymentAdapterType.DOCKER:
        raise DeploymentAdapterError(
            "Docker-адаптер пока не реализован."
        )

    if target.adapter_type == DeploymentAdapterType.SSH:
        raise DeploymentAdapterError(
            "SSH-адаптер пока не реализован."
        )

    if target.adapter_type == DeploymentAdapterType.WINDOWS_AGENT:
        raise DeploymentAdapterError(
            "Windows Agent пока не реализован."
        )

    raise DeploymentAdapterError(
        "Для указанного типа установки нет доступного адаптера."
    )
