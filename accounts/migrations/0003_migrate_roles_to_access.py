from django.db import migrations


VALID_ROLES = {
    "administrator",
    "approver",
    "executor",
}


def migrate_roles_to_access(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    RoleAssignment = apps.get_model("access", "RoleAssignment")

    db_alias = schema_editor.connection.alias

    users = User.objects.using(db_alias).all()

    for user in users.iterator():
        if user.role not in VALID_ROLES:
            raise ValueError(
                f"Unknown role '{user.role}' "
                f"for user '{user.username}'"
            )

        RoleAssignment.objects.using(db_alias).get_or_create(
            user_id=user.pk,
            defaults={
                "role": user.role,
                "assigned_by_id": None,
            },
        )


def restore_roles_to_user(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    RoleAssignment = apps.get_model("access", "RoleAssignment")

    db_alias = schema_editor.connection.alias

    assignments = RoleAssignment.objects.using(db_alias).all()

    for assignment in assignments.iterator():
        User.objects.using(db_alias).filter(
            pk=assignment.user_id
        ).update(
            role=assignment.role
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_alter_user_role"),
        ("access", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            migrate_roles_to_access,
            restore_roles_to_user,
        ),
    ]