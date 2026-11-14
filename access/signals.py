
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from .models import RoleAssignment


# Специальная группа для пользователей модуля Regulations.
WAGTAIL_ACCESS_GROUP_NAME = "Regulations — доступ в Wagtail"


def get_wagtail_access_group(using="default"):
    """
    Получает или создаёт группу и выдаёт ей разрешение
    на вход в административную панель Wagtail.
    """

    group, _ = Group.objects.using(using).get_or_create(
        name=WAGTAIL_ACCESS_GROUP_NAME
    )

    content_type, _ = ContentType.objects.using(using).get_or_create(
        app_label="wagtailadmin",
        model="admin",
    )

    permission, _ = Permission.objects.using(using).get_or_create(
        content_type=content_type,
        codename="access_admin",
        defaults={
            "name": "Can access Wagtail admin",
        },
    )

    group.permissions.add(permission)

    return group


def _remove_wagtail_access_if_unused(user_id, using="default"):
    """
    Убирает пользователя из группы Wagtail только тогда,
    когда у него больше не осталось назначенных ролей.
    """

    has_roles = RoleAssignment.objects.using(using).filter(
        user_id=user_id,
    ).exists()

    if has_roles:
        return

    group = Group.objects.using(using).filter(
        name=WAGTAIL_ACCESS_GROUP_NAME
    ).first()

    if not group:
        return

    User = get_user_model()

    user = (
        User.objects.using(using)
        .filter(pk=user_id)
        .first()
    )

    if user:
        group.user_set.remove(user)


@receiver(pre_save, sender=RoleAssignment)
def remember_previous_user(sender, instance, using, **kwargs):
    """
    Запоминает прежнего пользователя, если при редактировании
    назначения роли меняется сам пользователь.
    """

    if not instance.pk:
        return

    instance._previous_user_id = (
        sender.objects.using(using)
        .filter(pk=instance.pk)
        .values_list("user_id", flat=True)
        .first()
    )


@receiver(post_save, sender=RoleAssignment)
def grant_wagtail_access(sender, instance, using, **kwargs):
    """
    При создании или изменении назначения:

    1. Удаляет прежнего пользователя из группы,
       только если у него больше нет других ролей.
    2. Обеспечивает is_staff=True.
    3. Добавляет текущего пользователя в группу Wagtail.
    """

    group = get_wagtail_access_group(using)

    previous_user_id = getattr(
        instance,
        "_previous_user_id",
        None,
    )

    if (
        previous_user_id
        and previous_user_id != instance.user_id
    ):
        _remove_wagtail_access_if_unused(
            previous_user_id,
            using=using,
        )

    User = get_user_model()

    user = User.objects.using(using).get(
        pk=instance.user_id
    )

    if not user.is_staff:
        User.objects.using(using).filter(
            pk=user.pk,
        ).update(is_staff=True)

    group.user_set.add(user)


@receiver(post_delete, sender=RoleAssignment)
def remove_wagtail_access(sender, instance, using, **kwargs):
    """
    После удаления назначения проверяет, остались ли
    у пользователя другие роли.

    Если не осталось, удаляет его из группы Wagtail.
    """

    _remove_wagtail_access_if_unused(
        instance.user_id,
        using=using,
    )


def sync_wagtail_access_group(sender, using, **kwargs):
    """
    Синхронизирует специальную группу со всеми существующими
    назначениями ролей после выполнения миграций.
    """

    group = get_wagtail_access_group(using)

    User = get_user_model()

    user_ids = list(
        RoleAssignment.objects.using(using)
        .values_list("user_id", flat=True)
        .distinct()
    )

    User.objects.using(using).filter(
        pk__in=user_ids,
        is_staff=False,
    ).update(is_staff=True)

    users = User.objects.using(using).filter(
        pk__in=user_ids
    )

    group.user_set.set(users)
