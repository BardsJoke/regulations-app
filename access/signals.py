from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from .models import RoleAssignment


# Специальная группа для всех пользователей модуля Regulations.
WAGTAIL_ACCESS_GROUP_NAME = "Regulations — доступ в Wagtail"


def get_wagtail_access_group(using="default"):
    """
    Получает или создаёт группу и выдаёт ей разрешение
    на вход в административную панель Wagtail.
    """

    group, _ = Group.objects.using(using).get_or_create(
        name=WAGTAIL_ACCESS_GROUP_NAME
    )

    # Получаем ContentType модели Admin из приложения Wagtail.
    content_type, _ = ContentType.objects.using(using).get_or_create(
        app_label="wagtailadmin",
        model="admin",
    )

    # Получаем или создаём разрешение на вход в Wagtail.
    permission, _ = Permission.objects.using(using).get_or_create(
        content_type=content_type,
        codename="access_admin",
        defaults={
            "name": "Can access Wagtail admin",
        },
    )

    group.permissions.add(permission)

    return group


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
    При создании или изменении назначения роли:
    1. Добавляет пользователя в группу Wagtail.
    2. Обеспечивает is_staff=True.
    """

    group = get_wagtail_access_group(using)

    # Если в назначении заменили пользователя,
    # убираем прежнего из специальной группы.
    previous_user_id = getattr(instance, "_previous_user_id", None)

    if previous_user_id and previous_user_id != instance.user_id:
        old_user = (
            get_user_model().objects.using(using)
            .filter(pk=previous_user_id)
            .first()
        )

        if old_user:
            group.user_set.remove(old_user)

    User = get_user_model()

    user = User.objects.using(using).get(pk=instance.user_id)

    # Пользователь должен иметь статус сотрудника Django.
    if not user.is_staff:
        User.objects.using(using).filter(pk=user.pk).update(
            is_staff=True
        )

    # Выдаём доступ в Wagtail через группу.
    group.user_set.add(user)


@receiver(post_delete, sender=RoleAssignment)
def remove_wagtail_access(sender, instance, using, **kwargs):
    """
    Убирает пользователя из специальной группы,
    если его назначение роли удалено.
    """

    group = Group.objects.using(using).filter(
        name=WAGTAIL_ACCESS_GROUP_NAME
    ).first()

    if not group:
        return

    user = (
        get_user_model().objects.using(using)
        .filter(pk=instance.user_id)
        .first()
    )

    if user:
        group.user_set.remove(user)


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
    )

    # Все пользователи с назначенной ролью должны быть is_staff.
    User.objects.using(using).filter(
        pk__in=user_ids,
        is_staff=False,
    ).update(is_staff=True)

    # Группа должна содержать ровно пользователей с назначенными ролями.
    users = User.objects.using(using).filter(pk__in=user_ids)

    group.user_set.set(users)