
from django.contrib.auth import get_user_model
from django.db.models import Q

from wagtail.permission_policies import BasePermissionPolicy

from access.models import ModuleRole, RoleAssignment
from catalogs.models import CardStatusCode


class ChangeTicketPermissionPolicy(BasePermissionPolicy):
    """
    Политика доступа к карточкам изменений.

    Администратор:
        Просмотр всех карточек.

    Руководитель:
        Просмотр карточек на согласовании
        и согласованных карточек.

    Исполнитель:
        Просмотр собственных карточек,
        создание и редактирование собственных черновиков.

    Один пользователь может иметь несколько ролей.
    Права всех назначенных ролей объединяются.
    """

    def _get_roles(self, user):
        """
        Возвращает все роли активного пользователя.
        """

        if (
            not user
            or not getattr(user, "is_authenticated", False)
            or not getattr(user, "is_active", False)
        ):
            return set()

        return set(
            RoleAssignment.objects
            .filter(user_id=user.pk)
            .values_list("role", flat=True)
        )

    # ========================================================
    # ПРАВА ПОЛЬЗОВАТЕЛЕЙ
    # ========================================================

    def users_with_any_permission(self, actions):
        """
        Возвращает пользователей, имеющих хотя бы одно
        из запрошенных глобальных разрешений.
        """

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
            regulations_role_assignments__role__in=roles,
        ).distinct()

    def user_has_permission(self, user, action):
        """
        Проверяет глобальные права пользователя.
        """

        roles = self._get_roles(user)

        if action == "view":
            return bool(
                roles.intersection({
                    ModuleRole.ADMINISTRATOR,
                    ModuleRole.APPROVER,
                    ModuleRole.EXECUTOR,
                })
            )

        if action in {"add", "change"}:
            return ModuleRole.EXECUTOR in roles

        return False

    # ========================================================
    # ПРАВА НА ОТДЕЛЬНУЮ КАРТОЧКУ
    # ========================================================

    def user_has_permission_for_instance(
        self,
        user,
        action,
        instance,
    ):
        """
        Проверяет права пользователя на конкретную карточку.

        Если пользователь имеет несколько ролей,
        учитываются права каждой из них.
        """

        roles = self._get_roles(user)

        if action == "view":

            # Администратор видит все карточки.
            if ModuleRole.ADMINISTRATOR in roles:
                return True

            # Руководитель видит карточки на согласовании
            # и согласованные карточки.
            if (
                ModuleRole.APPROVER in roles
                and instance.status.code in {
                    CardStatusCode.PENDING,
                    CardStatusCode.APPROVED,
                }
            ):
                return True

            # Исполнитель видит собственные карточки.
            if (
                ModuleRole.EXECUTOR in roles
                and instance.created_by_id == user.pk
            ):
                return True

            return False

        if action == "change":
            return (
                ModuleRole.EXECUTOR in roles
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
        """
        Проверяет наличие хотя бы одного разрешения
        на конкретную карточку.
        """

        return any(
            self.user_has_permission_for_instance(
                user,
                action,
                instance,
            )
            for action in actions
        )

    # ========================================================
    # ФИЛЬТРАЦИЯ ДОСТУПНЫХ КАРТОЧЕК
    # ========================================================

    def instances_user_has_any_permission_for(
        self,
        user,
        actions,
    ):
        """
        Возвращает карточки, к которым у пользователя
        есть хотя бы одно из запрошенных разрешений.

        Учитываются все назначенные пользователю роли.
        """

        roles = self._get_roles(user)
        actions = set(actions)

        queryset = self.model.objects.select_related(
            "software",
            "status",
            "created_by",
        )

        conditions = []

        # ----------------------------------------------------
        # ПРОСМОТР
        # ----------------------------------------------------

        if "view" in actions:

            # Администратор видит все карточки.
            if ModuleRole.ADMINISTRATOR in roles:
                return queryset

            # Руководитель видит карточки на согласовании
            # и согласованные карточки.
            if ModuleRole.APPROVER in roles:
                conditions.append(
                    Q(
                        status__code__in=[
                            CardStatusCode.PENDING,
                            CardStatusCode.APPROVED,
                        ]
                    )
                )

            # Исполнитель видит все собственные карточки.
            if ModuleRole.EXECUTOR in roles:
                conditions.append(
                    Q(created_by_id=user.pk)
                )

        # ----------------------------------------------------
        # РЕДАКТИРОВАНИЕ
        # ----------------------------------------------------

        if (
            "change" in actions
            and ModuleRole.EXECUTOR in roles
        ):
            conditions.append(
                Q(
                    created_by_id=user.pk,
                    status__code=CardStatusCode.DRAFT,
                )
            )

        # ----------------------------------------------------
        # ОБЪЕДИНЕНИЕ ПРАВ РАЗНЫХ РОЛЕЙ
        # ----------------------------------------------------

        if not conditions:
            return queryset.none()

        combined_condition = conditions[0]

        for condition in conditions[1:]:
            combined_condition |= condition

        return queryset.filter(
            combined_condition
        ).distinct()

    def instances_user_has_permission_for(
        self,
        user,
        action,
    ):
        """
        Возвращает карточки, доступные для конкретного действия.
        """

        return self.instances_user_has_any_permission_for(
            user,
            {action},
        )
