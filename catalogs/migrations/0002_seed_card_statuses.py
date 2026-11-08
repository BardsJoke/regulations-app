from django.db import migrations


CARD_STATUSES = [
    ("draft", "Черновик", 10),
    ("pending", "На согласовании", 20),
    ("approved", "Согласовано", 30),
    ("in_progress", "В работе", 40),
    ("completed", "Успешно завершено", 50),
    ("rolled_back", "Откат изменений", 60),
    ("cancelled", "Отменено", 70),
]


def create_card_statuses(apps, schema_editor):
    CardStatus = apps.get_model("catalogs", "CardStatus")
    db_alias = schema_editor.connection.alias

    for code, name, sort_order in CARD_STATUSES:
        CardStatus.objects.using(db_alias).get_or_create(
            code=code,
            defaults={
                "name": name,
                "sort_order": sort_order,
                "is_active": True,
            },
        )


def remove_card_statuses(apps, schema_editor):
    CardStatus = apps.get_model("catalogs", "CardStatus")
    db_alias = schema_editor.connection.alias

    codes = [code for code, _, _ in CARD_STATUSES]

    CardStatus.objects.using(db_alias).filter(
        code__in=codes,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalogs", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_card_statuses,
            remove_card_statuses,
        ),
    ]