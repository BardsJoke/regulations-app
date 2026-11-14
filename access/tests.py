
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from audit.models import AuditEvent
from audit.services import AuditAction

from .models import ModuleRole, RoleAssignment
from .services import (
    RoleAssignmentWorkflowError,
    record_role_assigned,
    record_role_assignment_change,
    record_role_revoked,
)
from .signals import get_wagtail_access_group


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

    # -----------------------------------------------------
    # СИНХРОНИЗАЦИЯ С WAGTAIL
    # -----------------------------------------------------

    def test_user_keeps_wagtail_access_while_another_role_exists(self):
        """
        Удаление одного назначения не должно снимать доступ,
        если у пользователя остаётся другая роль.
        """

        group = get_wagtail_access_group()

        executor_assignment = RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.EXECUTOR,
        )

        approver_assignment = RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.APPROVER,
        )

        self.assertTrue(
            group.user_set.filter(pk=self.user.pk).exists()
        )

        executor_assignment.delete()

        self.assertTrue(
            group.user_set.filter(pk=self.user.pk).exists()
        )

        self.assertTrue(
            self.user.regulations_role_assignments.filter(
                pk=approver_assignment.pk,
            ).exists()
        )

        approver_assignment.delete()

        self.assertFalse(
            group.user_set.filter(pk=self.user.pk).exists()
        )

    def test_changing_assigned_user_preserves_old_users_other_roles(self):
        """
        При смене пользователя старый пользователь сохраняет
        доступ, если у него остаются другие назначения.
        """

        other_user = User.objects.create_user(
            username="replacement_user",
            email="replacement@example.test",
            password="test-password",
        )

        group = get_wagtail_access_group()

        assignment_to_change = RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.EXECUTOR,
        )

        RoleAssignment.objects.create(
            user=self.user,
            role=ModuleRole.APPROVER,
        )

        assignment_to_change.user = other_user
        assignment_to_change.save()

        # У прежнего пользователя осталась другая роль.
        self.assertTrue(
            group.user_set.filter(pk=self.user.pk).exists()
        )

        # Новый пользователь получил доступ.
        self.assertTrue(
            group.user_set.filter(pk=other_user.pk).exists()
        )

    # -----------------------------------------------------
    # АУДИТ
    # -----------------------------------------------------


class RoleAssignmentAuditTests(TestCase):

    def setUp(self):
        self.actor = User.objects.create_user(
            username="role_manager",
            email="role_manager@example.test",
            password="test-password",
        )

        self.target_user = User.objects.create_user(
            username="role_target",
            email="role_target@example.test",
            password="test-password",
        )

        self.other_user = User.objects.create_user(
            username="role_target_new",
            email="role_target_new@example.test",
            password="test-password",
        )

    def _get_event(self, *, assignment, action):
        return AuditEvent.objects.get(
            action=action,
            object_type="access.roleassignment",
            object_id=assignment.pk,
        )

    def test_role_assignment_creates_audit_event(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.EXECUTOR,
            assigned_by=self.actor,
        )

        event = record_role_assigned(
            assignment=assignment,
            actor=self.actor,
        )

        self.assertEqual(
            event.action,
            AuditAction.ROLE_ASSIGNED,
        )

        self.assertEqual(event.user, self.actor)

        self.assertEqual(
            event.details["subject_user_id"],
            str(self.target_user.pk),
        )

        self.assertEqual(
            event.details["role"],
            ModuleRole.EXECUTOR,
        )

    def test_changing_assignment_creates_revoked_and_assigned_events(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.EXECUTOR,
            assigned_by=self.actor,
        )

        old_user_id = assignment.user_id
        old_role = assignment.role

        assignment.user = self.other_user
        assignment.role = ModuleRole.APPROVER
        assignment.save()

        events = record_role_assignment_change(
            assignment=assignment,
            actor=self.actor,
            old_user_id=old_user_id,
            old_role=old_role,
        )

        self.assertEqual(len(events), 2)

        revoked_event = self._get_event(
            assignment=assignment,
            action=AuditAction.ROLE_REVOKED,
        )

        assigned_event = self._get_event(
            assignment=assignment,
            action=AuditAction.ROLE_ASSIGNED,
        )

        self.assertEqual(revoked_event.user, self.actor)
        self.assertEqual(assigned_event.user, self.actor)

        self.assertEqual(
            revoked_event.details["subject_user_id"],
            str(self.target_user.pk),
        )

        self.assertEqual(
            revoked_event.details["role"],
            ModuleRole.EXECUTOR,
        )

        self.assertEqual(
            assigned_event.details["subject_user_id"],
            str(self.other_user.pk),
        )

        self.assertEqual(
            assigned_event.details["role"],
            ModuleRole.APPROVER,
        )

    def test_unchanged_assignment_does_not_create_audit_events(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.EXECUTOR,
        )

        events = record_role_assignment_change(
            assignment=assignment,
            actor=self.actor,
            old_user_id=assignment.user_id,
            old_role=assignment.role,
        )

        self.assertEqual(events, [])

        self.assertFalse(
            AuditEvent.objects.filter(
                object_type="access.roleassignment",
                object_id=assignment.pk,
            ).exists()
        )

    def test_revoking_role_creates_audit_event(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.EXECUTOR,
            assigned_by=self.actor,
        )

        event = record_role_revoked(
            assignment=assignment,
            actor=self.actor,
        )

        self.assertEqual(
            event.action,
            AuditAction.ROLE_REVOKED,
        )

        self.assertEqual(event.user, self.actor)

        self.assertEqual(
            event.details["subject_user_id"],
            str(self.target_user.pk),
        )

        # Само удаление выполняется представлением Wagtail.
        self.assertTrue(
            RoleAssignment.objects.filter(
                pk=assignment.pk,
            ).exists()
        )

    def test_last_administrator_role_cannot_be_revoked(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.ADMINISTRATOR,
        )

        with self.assertRaises(RoleAssignmentWorkflowError):
            record_role_revoked(
                assignment=assignment,
                actor=self.actor,
            )

        self.assertTrue(
            RoleAssignment.objects.filter(
                pk=assignment.pk,
            ).exists()
        )

        self.assertFalse(
            AuditEvent.objects.filter(
                action=AuditAction.ROLE_REVOKED,
                object_id=assignment.pk,
            ).exists()
        )

    def test_administrator_can_be_revoked_when_another_admin_exists(self):
        assignment = RoleAssignment.objects.create(
            user=self.target_user,
            role=ModuleRole.ADMINISTRATOR,
        )

        RoleAssignment.objects.create(
            user=self.other_user,
            role=ModuleRole.ADMINISTRATOR,
        )

        event = record_role_revoked(
            assignment=assignment,
            actor=self.actor,
        )

        self.assertEqual(
            event.action,
            AuditAction.ROLE_REVOKED,
        )

        self.assertEqual(event.user, self.actor)
