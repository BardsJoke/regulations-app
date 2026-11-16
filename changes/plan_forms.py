
from django import forms

from .models import StepKind


class PlanStepForm(forms.Form):
    """Форма добавления и редактирования планового шага."""

    step_kind = forms.ChoiceField(
        label="Тип шага",
        choices=StepKind.choices,
    )

    name = forms.CharField(
        label="Название шага",
        max_length=200,
    )

    description = forms.CharField(
        label="Описание",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    planned_duration_min = forms.IntegerField(
        label="Плановая длительность (минуты)",
        min_value=1,
    )

    def __init__(self, *args, ticket, step=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.ticket = ticket
        self.step = step

        # При редактировании заполняем текущие значения.
        if step is not None:
            self.initial.update({
                "step_kind": step.step_kind,
                "name": step.name,
                "description": step.description,
                "planned_duration_min": step.planned_duration_min,
            })

        # Первый шаг всегда должен быть резервным копированием.
        is_first_step = (
            step.step_order == 1
            if step is not None
            else not ticket.plan_steps.exists()
        )

        if is_first_step:
            backup_choice = next(
                choice
                for choice in StepKind.choices
                if choice[0] == StepKind.BACKUP
            )

            self.fields["step_kind"].choices = [
                backup_choice
            ]

        self.fields["step_kind"].widget.attrs.update({
            "class": "w-field"
        })

        self.fields["name"].widget.attrs.update({
            "placeholder": "Например: Резервное копирование Museum Demo"
        })

        self.fields["description"].widget.attrs.update({
            "placeholder": "Опишите, что необходимо выполнить"
        })

        self.fields["planned_duration_min"].widget.attrs.update({
            "min": 1,
        })
