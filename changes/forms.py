from django import forms
from django.db.models import Q

from catalogs.models import MuseumSoftware

from .models import ChangeTicket


class ChangeTicketForm(forms.ModelForm):
    """
    Форма создания и редактирования черновика.

    Системные поля — автор, статус, результаты проверок
    и контрольные суммы — пользователь не редактирует.
    """

    class Meta:
        model = ChangeTicket

        fields = [
            "software",
            "source_version",
            "target_version",
            "risks",
            "window_start_at",
            "window_end_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        software_queryset = MuseumSoftware.objects.filter(
            is_active=True,
        )

        # При редактировании оставляем возможность сохранить
        # ранее выбранное ПО, даже если оно стало неактивным.
        if self.instance.pk and self.instance.software_id:
            software_queryset = MuseumSoftware.objects.filter(
                Q(is_active=True)
                | Q(pk=self.instance.software_id)
            )

        self.fields["software"].queryset = software_queryset.order_by(
            "name"
        )