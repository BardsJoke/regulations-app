from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from .models import ModuleRole, RoleAssignment


User = get_user_model()


class RoleAssignmentTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username="multi_role_user",
            email="multi_role@example.test",
            password="test-password",
        )

    def test_user_can_have_multiple_roles(self):
        """Один пользователь может иметь несколько разных ролей."""

        RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.EXECUTOR,
        )

        RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.APPROVER,
        )

        roles = set(
            self.user.regulations_role_assignments.values_list(
                "role",
                flat=True,
            )
        )

        self.assertEqual(
            roles,
            {
                ModuleRole.EXECUTOR,
                ModuleRole.APPROVER,
            },
        )

        self.assertEqual(
            self.user.regulations_role_assignments.count(),
            2,
        )

    def test_duplicate_role_assignment_is_forbidden(self):
        """Нельзя дважды назначить одну роль одному пользователю."""

        RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.EXECUTOR,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleAssignment.objects.create(
                    user=self.user,
                    role=ModuleRole.EXECUTOR,
                )

    def test_user_cannot_be_deleted_with_assigned_roles(self):
        """Пользователя с назначенными ролями нельзя удалить."""

        RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.EXECUTOR,
        )

        with self.assertRaises(ProtectedError):
            self.user.delete()

        self.assertTrue(
            User.objects.filter(pk=self.user.pk).exists()
        )