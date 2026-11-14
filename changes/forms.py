
from django import forms

from deployments.models import (
    CONFIGURED_ADAPTER_TYPES,
    DeploymentEnvironment,
    DeploymentTarget,
)

from .models import ChangeTicket


class ChangeTicketForm(forms.ModelForm):
    """
    Форма создания и редактирования черновика.

    Пользователь выбирает конкретную целевую установку.
    Программное обеспечение определяется автоматически
    через выбранную установку.

    Системные поля — автор, статус, результаты проверок
    и контрольные суммы — пользователь не редактирует.
    """

    class Meta:
        model = ChangeTicket

        fields = [
            "deployment_target",
            "source_version",
            "target_version",
            "risks",
            "window_start_at",
            "window_end_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        available_targets = (
            DeploymentTarget.objects
            .select_related("software")
            .filter(
                is_active=True,
                software__is_active=True,
                adapter_type__in=CONFIGURED_ADAPTER_TYPES,
            )
            .exclude(
                environment=DeploymentEnvironment.UNKNOWN,
            )
            .exclude(config_key="")
            .order_by(
                "software__name",
                "environment",
                "name",
            )
        )

        self.fields["deployment_target"].queryset = available_targets
        self.fields["deployment_target"].required = True

        self.fields["deployment_target"].label = (
            "Целевая установка"
        )

        self.fields["deployment_target"].help_text = (
            "Выберите конкретную установку программного обеспечения, "
            "для которой планируется изменение."
        )
