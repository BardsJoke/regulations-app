from django.contrib.auth import get_user_model

from wagtail.permission_policies import BasePermissionPolicy

from access.models import ModuleRole, RoleAssignment
from catalogs.models import CardStatusCode


class ChangeTicketPermissionPolicy(BasePermissionPolicy):
    """
    Политика доступа к карточкам изменений.

    Администратор: просмотр.
    Руководитель: просмотр карточек на согласовании
                  и согласованных карточек.
    Исполнитель: просмотр собственных карточек,
                 создание и редактирование собственных черновиков.
    """

    def _get_role(self, user):
        if (
            not user
            or not getattr(user, "is_authenticated", False)
            or not getattr(user, "is_active", False)
        ):
            return None

        return (
            RoleAssignment.objects
            .filter(user_id=user.pk)
            .values_list("role", flat=True)
            .first()
        )

    def users_with_any_permission(self, actions):
        actions = set(actions)
        roles = set()

        if "view" in actions:
            roles.update({
                ModuleRole.ADMINISTRATOR,
                ModuleRole.APPROVER,
                ModuleRole.EXECUTOR,
            })

        if actions.intersection({"add", "change"}):
            roles.add(ModuleRole.EXECUTOR)

        if not roles:
            return get_user_model().objects.none()

        return get_user_model().objects.filter(
            is_active=True,
            regulations_role__role__in=roles,
        ).distinct()

    def user_has_permission(self, user, action):
        role = self._get_role(user)

        if action == "view":
            return role in ModuleRole.values

        if action in {"add", "change"}:
            return role == ModuleRole.EXECUTOR

        return False

    def user_has_permission_for_instance(
        self,
        user,
        action,
        instance,
    ):
        role = self._get_role(user)

        if action == "view":
            if role == ModuleRole.ADMINISTRATOR:
                return True

            if role == ModuleRole.APPROVER:
                return instance.status.code in {
                    CardStatusCode.PENDING,
                    CardStatusCode.APPROVED,
                }

            if role == ModuleRole.EXECUTOR:
                return instance.created_by_id == user.pk

        if action == "change":
            return (
                role == ModuleRole.EXECUTOR
                and instance.created_by_id == user.pk
                and instance.status.code == CardStatusCode.DRAFT
            )

        return False

    def user_has_any_permission_for_instance(
        self,
        user,
        actions,
        instance,
    ):
        return any(
            self.user_has_permission_for_instance(
                user,
                action,
                instance,
            )
            for action in actions
        )

    def instances_user_has_any_permission_for(self, user, actions):
        """
        Возвращает только те карточки, которые пользователь
        имеет право видеть или изменять.
        """

        role = self._get_role(user)

        queryset = self.model.objects.select_related(
            "software",
            "status",
            "created_by",
        )

        actions = set(actions)

        if "view" in actions:
            if role == ModuleRole.ADMINISTRATOR:
                return queryset

            if role == ModuleRole.APPROVER:
                return queryset.filter(
                    status__code__in=[
                        CardStatusCode.PENDING,
                        CardStatusCode.APPROVED,
                    ],
                )

            if role == ModuleRole.EXECUTOR:
                return queryset.filter(
                    created_by_id=user.pk,
                )

        if (
            "change" in actions
            and role == ModuleRole.EXECUTOR
        ):
            return queryset.filter(
                created_by_id=user.pk,
                status__code=CardStatusCode.DRAFT,
            )

        return queryset.none()

    def instances_user_has_permission_for(self, user, action):
        return self.instances_user_has_any_permission_for(
            user,
            {action},
        )