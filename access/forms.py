from django import forms

from .models import ModuleRole, RoleAssignment


class RoleAssignmentForm(forms.ModelForm):
    """
    Форма управления назначениями ролей.

    Не позволяет снять роль администратора
    с последнего администратора модуля.
    """

    class Meta:
        model = RoleAssignment
        fields = [
            "user",
            "role",
        ]

    def clean(self):
        cleaned_data = super().clean()

        if not self.instance.pk:
            return cleaned_data

        new_role = cleaned_data.get("role")

        old_assignment = (
            RoleAssignment.objects
            .filter(pk=self.instance.pk)
            .only("role")
            .first()
        )

        if old_assignment is None:
            return cleaned_data

        is_last_admin = (
            old_assignment.role == ModuleRole.ADMINISTRATOR
            and new_role != ModuleRole.ADMINISTRATOR
            and not RoleAssignment.objects.filter(
                role=ModuleRole.ADMINISTRATOR
            ).exclude(
                pk=self.instance.pk
            ).exists()
        )

        if is_last_admin:
            self.add_error(
                "role",
                "Нельзя снять роль с последнего администратора. "
                "Сначала назначьте другого пользователя администратором.",
            )

        return cleaned_data