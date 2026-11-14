
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment
from audit.models import AuditEvent
from audit.services import AuditAction
from catalogs.models import (
    CardStatus,
    CardStatusCode,
    MuseumSoftware,
    SoftwareType,
)

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeTicket,
    PlanStep,
    StepKind,
)
from .permissions import ChangeTicketPermissionPolicy
from .services import (
    ChangeWorkflowError,
    approve_ticket,
    cancel_ticket,
    create_draft,
    reject_ticket,
    reschedule_ticket,
    submit_for_approval,
    update_draft,
)


User = get_user_model()


class ChangeWorkflowTests(TestCase):
    """
    Проверка основных правил бизнес-процесса карточек изменений.
    """

    def setUp(self):
        # Создаём тестовых пользователей.
        self.executor = User.objects.create_user(
            username="test_executor",
            email="executor@example.test",
            password="test-password",
        )

        self.approver = User.objects.create_user(
            username="test_approver",
            email="approver@example.test",
            password="test-password",
        )

        # Назначаем роли внутри модуля.
        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        RoleAssignment.objects.create(
            user=self.approver,
            role=ModuleRole.APPROVER,
        )

        # Создаём тестовое программное обеспечение.
        software_type = SoftwareType.objects.create(
            name="Тестовый тип ПО",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=software_type,
            name="Тестовая музейная система",
            current_version="1.0",
        )

        # Плановое окно для тестов.
        self.window_start = timezone.now() + timedelta(days=2)
        self.window_end = self.window_start + timedelta(hours=2)

    def create_test_draft(self, *, add_backup_step=True):
        """
        Создаёт черновик и при необходимости
        добавляет обязательный шаг резервного копирования.
        """

        ticket = create_draft(
            actor=self.executor,
            software=self.software,
            source_version="1.0",
            target_version="1.1",
            risks="Тестовое описание рисков",
            window_start_at=self.window_start,
            window_end_at=self.window_end,
        )

        if add_backup_step:
            PlanStep.objects.create(
                ticket=ticket,
                step_order=1,
                step_kind=StepKind.BACKUP,
                name="Создать резервную копию",
                planned_duration_min=15,
            )

        return ticket

    def submit_test_draft(self):
        """Создаёт черновик и отправляет его на согласование."""

        ticket = self.create_test_draft()

        return submit_for_approval(
            ticket_id=ticket.pk,
            actor=self.executor,
        )

    def _assert_audit_event(self, *, ticket, actor, action):
        """Проверяет наличие события аудита для карточки."""

        event = AuditEvent.objects.get(
            action=action,
            object_type="changes.changeticket",
            object_id=ticket.pk,
        )

        self.assertEqual(event.user, actor)

        return event

    # -----------------------------------------------------
    # СОЗДАНИЕ КАРТОЧКИ
    # -----------------------------------------------------

    def test_executor_can_create_draft(self):
        ticket = self.create_test_draft()

        self.assertEqual(
            ticket.status.code,
            CardStatusCode.DRAFT,
        )

        self.assertEqual(
            ticket.created_by,
            self.executor,
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.executor,
            action=AuditAction.CHANGE_CREATED,
        )

    def test_approver_cannot_create_draft(self):
        with self.assertRaises(PermissionDenied):
            create_draft(
                actor=self.approver,
                software=self.software,
            )

    # -----------------------------------------------------
    # РЕДАКТИРОВАНИЕ ЧЕРНОВИКА
    # -----------------------------------------------------

    def test_executor_can_update_draft(self):
        ticket = self.create_test_draft()

        result = update_draft(
            ticket_id=ticket.pk,
            actor=self.executor,
            software=self.software,
            source_version="1.0",
            target_version="1.2",
            risks="Обновлённое описание рисков",
            window_start_at=self.window_start,
            window_end_at=self.window_end,
        )

        self.assertEqual(
            result.target_version,
            "1.2",
        )

        self.assertEqual(
            result.risks,
            "Обновлённое описание рисков",
        )

        event = self._assert_audit_event(
            ticket=ticket,
            actor=self.executor,
            action=AuditAction.CHANGE_UPDATED,
        )

        self.assertEqual(
            set(event.details["changed_fields"]),
            {"target_version", "risks"},
        )

    # -----------------------------------------------------
    # ОТПРАВКА НА СОГЛАСОВАНИЕ
    # -----------------------------------------------------

    def test_executor_can_submit_draft(self):
        ticket = self.submit_test_draft()

        self.assertEqual(
            ticket.status.code,
            CardStatusCode.PENDING,
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.executor,
            action=AuditAction.CHANGE_SUBMITTED,
        )

    def test_submission_requires_backup_step(self):
        ticket = self.create_test_draft(
            add_backup_step=False,
        )

        with self.assertRaises(ChangeWorkflowError):
            submit_for_approval(
                ticket_id=ticket.pk,
                actor=self.executor,
            )

        ticket.refresh_from_db()

        self.assertEqual(
            ticket.status.code,
            CardStatusCode.DRAFT,
        )

        # Неуспешная отправка не должна создавать событие.
        self.assertFalse(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_SUBMITTED,
                object_type="changes.changeticket",
                object_id=ticket.pk,
            ).exists()
        )

    def test_executor_cannot_submit_another_users_ticket(self):
        another_executor = User.objects.create_user(
            username="another_executor",
            email="another@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=another_executor,
            role=ModuleRole.EXECUTOR,
        )

        ticket = self.create_test_draft()

        with self.assertRaises(PermissionDenied):
            submit_for_approval(
                ticket_id=ticket.pk,
                actor=another_executor,
            )

    # -----------------------------------------------------
    # СОГЛАСОВАНИЕ
    # -----------------------------------------------------

    def test_approver_can_approve_ticket(self):
        ticket = self.submit_test_draft()

        result = approve_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            comment="План согласован.",
        )

        self.assertEqual(
            result.status.code,
            CardStatusCode.APPROVED,
        )

        self.assertEqual(
            result.approved_by,
            self.approver,
        )

        self.assertEqual(
            ApprovalRecord.objects.filter(
                ticket=ticket,
                decision=ApprovalDecision.APPROVED,
            ).count(),
            1,
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.approver,
            action=AuditAction.CHANGE_APPROVED,
        )

    def test_approver_cannot_approve_own_ticket(self):
        ticket = ChangeTicket.objects.create(
            software=self.software,
            created_by=self.approver,
            status=CardStatus.objects.get(
                code=CardStatusCode.PENDING,
            ),
        )

        with self.assertRaises(PermissionDenied):
            approve_ticket(
                ticket_id=ticket.pk,
                actor=self.approver,
            )

        self.assertFalse(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_APPROVED,
                object_id=ticket.pk,
            ).exists()
        )

    # -----------------------------------------------------
    # ОТКЛОНЕНИЕ
    # -----------------------------------------------------

    def test_rejection_returns_ticket_to_draft(self):
        ticket = self.submit_test_draft()

        result = reject_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            comment="Необходимо уточнить план резервного копирования.",
        )

        self.assertEqual(
            result.status.code,
            CardStatusCode.DRAFT,
        )

        self.assertEqual(
            result.rejection_comment,
            "Необходимо уточнить план резервного копирования.",
        )

        self.assertEqual(
            ApprovalRecord.objects.filter(
                ticket=ticket,
                decision=ApprovalDecision.REJECTED,
            ).count(),
            1,
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.approver,
            action=AuditAction.CHANGE_REJECTED,
        )

    def test_rejection_requires_comment(self):
        ticket = self.submit_test_draft()

        with self.assertRaises(ChangeWorkflowError):
            reject_ticket(
                ticket_id=ticket.pk,
                actor=self.approver,
                comment="   ",
            )

        ticket.refresh_from_db()

        self.assertEqual(
            ticket.status.code,
            CardStatusCode.PENDING,
        )

        self.assertEqual(
            ApprovalRecord.objects.filter(ticket=ticket).count(),
            0,
        )

        # Неуспешное отклонение не должно попадать
        # в журнал как завершённое действие.
        self.assertFalse(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_REJECTED,
                object_type="changes.changeticket",
                object_id=ticket.pk,
            ).exists()
        )

    # -----------------------------------------------------
    # ПЕРЕНОС ПЛАНОВОГО ОКНА
    # -----------------------------------------------------

    def test_reschedule_from_pending_approves_ticket(self):
        ticket = self.submit_test_draft()

        new_start = self.window_start + timedelta(days=3)
        new_end = new_start + timedelta(hours=3)

        result = reschedule_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            new_start_at=new_start,
            new_end_at=new_end,
            comment="Согласовано с новым временем.",
        )

        self.assertEqual(
            result.status.code,
            CardStatusCode.APPROVED,
        )

        self.assertEqual(
            result.window_start_at,
            new_start,
        )

        self.assertEqual(
            result.window_end_at,
            new_end,
        )

        record = ApprovalRecord.objects.get(
            ticket=ticket,
            decision=ApprovalDecision.RESCHEDULED,
        )

        self.assertEqual(
            record.old_start_at,
            self.window_start,
        )

        self.assertEqual(
            record.new_start_at,
            new_start,
        )

        event = self._assert_audit_event(
            ticket=ticket,
            actor=self.approver,
            action=AuditAction.CHANGE_RESCHEDULED,
        )

        self.assertEqual(
            event.details["approval_record_id"],
            str(record.pk),
        )

    def test_reschedule_approved_ticket_keeps_approved_status(self):
        ticket = self.submit_test_draft()

        approve_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
        )

        new_start = self.window_start + timedelta(days=5)
        new_end = new_start + timedelta(hours=2)

        result = reschedule_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            new_start_at=new_start,
            new_end_at=new_end,
        )

        self.assertEqual(
            result.status.code,
            CardStatusCode.APPROVED,
        )

        self.assertEqual(
            ApprovalRecord.objects.filter(
                ticket=ticket,
                decision=ApprovalDecision.RESCHEDULED,
            ).count(),
            1,
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.approver,
            action=AuditAction.CHANGE_RESCHEDULED,
        )

    # -----------------------------------------------------
    # ОТМЕНА
    # -----------------------------------------------------

    def test_approver_can_cancel_pending_ticket(self):
        ticket = self.submit_test_draft()

        result = cancel_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            comment="Изменение больше не требуется.",
        )

        self.assertEqual(
            result.status.code,
            CardStatusCode.CANCELLED,
        )

        self.assertTrue(
            ApprovalRecord.objects.filter(
                ticket=ticket,
                decision=ApprovalDecision.CANCELLED,
            ).exists()
        )

        self._assert_audit_event(
            ticket=ticket,
            actor=self.approver,
            action=AuditAction.CHANGE_CANCELLED,
        )

    # -----------------------------------------------------
    # ИСТОРИЯ СОГЛАСОВАНИЙ
    # -----------------------------------------------------

    def test_approval_history_is_not_overwritten(self):
        ticket = self.submit_test_draft()

        reject_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            comment="Нужно уточнить риски.",
        )

        # Исполнитель повторно отправляет карточку.
        submit_for_approval(
            ticket_id=ticket.pk,
            actor=self.executor,
        )

        approve_ticket(
            ticket_id=ticket.pk,
            actor=self.approver,
            comment="Теперь всё корректно.",
        )

        decisions = list(
            ApprovalRecord.objects
            .filter(ticket=ticket)
            .order_by("decided_at")
            .values_list("decision", flat=True)
        )

        self.assertEqual(
            decisions,
            [
                ApprovalDecision.REJECTED,
                ApprovalDecision.APPROVED,
            ],
        )

        # История аудита также сохраняет оба решения.
        self.assertEqual(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_REJECTED,
                object_type="changes.changeticket",
                object_id=ticket.pk,
            ).count(),
            1,
        )

        self.assertEqual(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_APPROVED,
                object_type="changes.changeticket",
                object_id=ticket.pk,
            ).count(),
            1,
        )

        self.assertEqual(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_SUBMITTED,
                object_type="changes.changeticket",
                object_id=ticket.pk,
            ).count(),
            2,
        )


class ChangeTicketMultiRolePermissionTests(TestCase):

    def setUp(self):
        self.executor = User.objects.create_user(
            username="multi_role_executor",
            email="multi_role_executor@example.test",
            password="test-password",
        )

        self.other_user = User.objects.create_user(
            username="other_executor",
            email="other_executor@example.test",
            password="test-password",
        )

        # Один пользователь имеет две роли.
        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.APPROVER,
        )

        software_type = SoftwareType.objects.create(
            name="Категория нескольких ролей",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=software_type,
            name="Тестовое музейное ПО",
            current_version="1.0",
        )

        # Собственная карточка исполнителя.
        self.own_ticket = ChangeTicket.objects.create(
            software=self.software,
            created_by=self.executor,
            status=CardStatus.objects.get(
                code=CardStatusCode.DRAFT,
            ),
            source_version="1.0",
            target_version="2.0",
        )

        # Чужая карточка на согласовании.
        self.other_ticket = ChangeTicket.objects.create(
            software=self.software,
            created_by=self.other_user,
            status=CardStatus.objects.get(
                code=CardStatusCode.PENDING,
            ),
            source_version="1.0",
            target_version="2.0",
        )

        self.policy = ChangeTicketPermissionPolicy(ChangeTicket)

    def test_user_receives_permissions_from_both_roles(self):

        # Глобальные права объединяются.
        self.assertTrue(
            self.policy.user_has_permission(
                self.executor,
                "view",
            )
        )

        self.assertTrue(
            self.policy.user_has_permission(
                self.executor,
                "change",
            )
        )

        # Исполнитель может редактировать собственный черновик.
        self.assertTrue(
            self.policy.user_has_permission_for_instance(
                self.executor,
                "change",
                self.own_ticket,
            )
        )

        # Руководитель может просматривать чужую карточку
        # на согласовании.
        self.assertTrue(
            self.policy.user_has_permission_for_instance(
                self.executor,
                "view",
                self.other_ticket,
            )
        )

        # Но не может редактировать чужую карточку.
        self.assertFalse(
            self.policy.user_has_permission_for_instance(
                self.executor,
                "change",
                self.other_ticket,
            )
        )

        # Проверяем объединение карточек в queryset.
        visible_tickets = (
            self.policy.instances_user_has_any_permission_for(
                self.executor,
                {"view"},
            )
        )

        visible_ids = set(
            visible_tickets.values_list("pk", flat=True)
        )

        self.assertSetEqual(
            visible_ids,
            {
                self.own_ticket.pk,
                self.other_ticket.pk,
            },
        )
