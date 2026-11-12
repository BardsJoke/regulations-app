from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from catalogs.models import (
    CardStatus,
    CardStatusCode,
    MuseumSoftware,
    RollbackReason,
    SoftwareType,
)

from changes.models import PlanStep, StepKind, ChangeTicket

from .models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
    StepTemplate,
)

from django.core.exceptions import PermissionDenied

from access.models import ModuleRole, RoleAssignment

from audit.models import AuditEvent
from audit.services import AuditAction

from .services import (
    OperationServiceError,
    start_step,
    complete_step,
    fail_step,
    start_update_operation,
)

import hashlib
import tempfile

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from changes.models import (
    CompatibilityStatus,
    HashStatus,
    ResourcesCheckStatus,
)

from backups.models import Backup, BackupIntegrityStatus

User = get_user_model()


class OperationsModelTests(TestCase):

    def setUp(self):
        # Пользователь для тестов.
        self.user = User.objects.create_user(
            username="executor_test",
            email="executor_test@example.test",
            password="test-password",
        )

        # Тестовое программное обеспечение.
        self.software_type = SoftwareType.objects.create(
            name="Тестовая категория ПО",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=self.software_type,
            name="Тестовое музейное ПО",
            current_version="1.0",
        )

        # Используем системный статус карточки.
        self.approved_status = CardStatus.objects.get(
            code=CardStatusCode.APPROVED,
        )

        # Причина отката.
        self.rollback_reason = RollbackReason.objects.create(
            name="Тестовая причина отката",
        )

        self.ticket = self.create_ticket()

    def create_ticket(self):
        return ChangeTicket.objects.create(
            software=self.software,
            created_by=self.user,
            status=self.approved_status,
            source_version="1.0",
            target_version="2.0",
        )

    def create_update_operation(
        self,
        ticket=None,
        sequence_number=1,
        attempt_number=1,
        status=OperationStatus.RUNNING,
    ):
        ticket = ticket or self.ticket

        finished_at = (
            None
            if status == OperationStatus.RUNNING
            else timezone.now()
        )

        return Operation.objects.create(
            change_ticket=ticket,
            operation_type=OperationType.UPDATE,
            sequence_number=sequence_number,
            attempt_number=attempt_number,
            initiated_by=self.user,
            status=status,
            finished_at=finished_at,
        )

    def create_rollback_operation(
        self,
        parent_operation,
        sequence_number=2,
        attempt_number=1,
    ):
        """
        Создаёт тестовую резервную копию с подтверждённой
        целостностью и связанную с ней операцию отката.
        """

        backup = Backup.objects.create(
            operation=parent_operation,
            file="test-backups/test-backup.bin",
            file_sha256="a" * 64,
            size_bytes=1024,
            created_by=self.user,
            integrity_status=BackupIntegrityStatus.VALID,
            verified_at=timezone.now(),
            verification_error="",
        )

        return Operation.objects.create(
            change_ticket=parent_operation.change_ticket,
            operation_type=OperationType.ROLLBACK,
            sequence_number=sequence_number,
            attempt_number=attempt_number,
            parent_operation=parent_operation,
            backup_used=backup,
            rollback_reason=self.rollback_reason,
            initiated_by=self.user,
        )

    def create_backup_plan_step(self, ticket=None):
        ticket = ticket or self.ticket

        return PlanStep.objects.create(
            ticket=ticket,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name="Создать резервную копию",
            description="Тестовый шаг резервного копирования",
            planned_duration_min=10,
        )

    # --------------------------------------------------------
    # OPERATION
    # --------------------------------------------------------

    def test_update_operation_can_be_created(self):
        operation = self.create_update_operation()

        self.assertEqual(
            operation.operation_type,
            OperationType.UPDATE,
        )

        self.assertIsNone(operation.parent_operation_id)
        self.assertIsNone(operation.rollback_reason_id)

    def test_rollback_must_reference_update_from_same_ticket(self):
        other_ticket = self.create_ticket()

        other_update = self.create_update_operation(
            ticket=other_ticket,
            status=OperationStatus.FAILED,
        )

        # Создаём резервную копию именно для другой операции UPDATE.
        other_backup = Backup.objects.create(
            operation=other_update,
            file="test-backups/other-backup.bin",
            file_sha256="a" * 64,
            size_bytes=1024,
            created_by=self.user,
            integrity_status=BackupIntegrityStatus.VALID,
            verified_at=timezone.now(),
            verification_error="",
        )

        rollback = Operation(
            change_ticket=self.ticket,
            operation_type=OperationType.ROLLBACK,
            sequence_number=2,
            attempt_number=1,
            parent_operation=other_update,
            rollback_reason=self.rollback_reason,
            initiated_by=self.user,
            backup_used=other_backup,
        )

        with self.assertRaises(ValidationError):
            rollback.full_clean()

    def test_rollback_cannot_reference_another_rollback(self):
        update = self.create_update_operation(
            status=OperationStatus.FAILED,
        )

        first_rollback = self.create_rollback_operation(
            parent_operation=update,
        )

        second_rollback = Operation(
            change_ticket=self.ticket,
            operation_type=OperationType.ROLLBACK,
            sequence_number=3,
            attempt_number=2,
            parent_operation=first_rollback,
            rollback_reason=self.rollback_reason,
            initiated_by=self.user,
            backup_used=first_rollback.backup_used,
        )

        with self.assertRaises(ValidationError):
            second_rollback.full_clean()

    def test_database_prevents_duplicate_attempt_number(self):
        self.create_update_operation(
            sequence_number=1,
            attempt_number=1,
            status=OperationStatus.FAILED,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_update_operation(
                    sequence_number=2,
                    attempt_number=1,
                    status=OperationStatus.FAILED,
                )

    def test_database_allows_only_one_running_operation(self):
        self.create_update_operation(
            sequence_number=1,
            attempt_number=1,
            status=OperationStatus.RUNNING,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_update_operation(
                    sequence_number=2,
                    attempt_number=2,
                    status=OperationStatus.RUNNING,
                )

    # --------------------------------------------------------
    # STEP EXECUTION
    # --------------------------------------------------------

    def test_update_step_execution_uses_plan_step(self):
        operation = self.create_update_operation()
        plan_step = self.create_backup_plan_step()

        execution = StepExecution(
            operation=operation,
            plan_step=plan_step,
            step_order=plan_step.step_order,
            step_kind=plan_step.step_kind,
            name=plan_step.name,
            description=plan_step.description,
            planned_duration_min=plan_step.planned_duration_min,
        )

        execution.full_clean()
        execution.save()

        self.assertEqual(execution.plan_step_id, plan_step.pk)
        self.assertEqual(execution.status, StepExecutionStatus.LOCKED)

    def test_execution_snapshot_cannot_be_changed(self):
        operation = self.create_update_operation()
        plan_step = self.create_backup_plan_step()

        execution = StepExecution.objects.create(
            operation=operation,
            plan_step=plan_step,
            step_order=plan_step.step_order,
            step_kind=plan_step.step_kind,
            name=plan_step.name,
            description=plan_step.description,
            planned_duration_min=plan_step.planned_duration_min,
        )

        execution.name = "Изменённое название"

        with self.assertRaises(ValidationError):
            execution.save()

    def test_rollback_step_does_not_require_plan_step(self):
        update = self.create_update_operation(
            status=OperationStatus.FAILED,
        )

        rollback = self.create_rollback_operation(
            parent_operation=update,
        )

        execution = StepExecution(
            operation=rollback,
            step_order=1,
            step_kind=StepKind.OTHER,
            name="Восстановить систему",
            description="Тестовый шаг отката",
            planned_duration_min=15,
        )

        execution.full_clean()
        execution.save()

        self.assertIsNone(execution.plan_step_id)

    # --------------------------------------------------------
    # STEP TEMPLATE
    # --------------------------------------------------------

    def test_step_template_can_be_created(self):
        template = StepTemplate.objects.create(
            step_kind=StepKind.VERIFICATION,
            name="Проверка работоспособности",
            description="Проверка после обновления",
            default_duration_min=15,
        )

        self.assertEqual(
            template.step_kind,
            StepKind.VERIFICATION,
        )

    def test_database_rejects_invalid_template_kind(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                StepTemplate.objects.create(
                    step_kind="unknown",
                    name="Некорректный шаблон",
                    default_duration_min=10,
                )
class OperationServiceTests(TestCase):

    def setUp(self):
        # Исполнитель.
        self.executor = User.objects.create_user(
            username="service_executor",
            email="service_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        # Руководитель для проверки ограничений доступа.
        self.approver = User.objects.create_user(
            username="service_approver",
            email="service_approver@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.approver,
            role=ModuleRole.APPROVER,
        )

        # Программное обеспечение.
        self.software_type = SoftwareType.objects.create(
            name="Сервисная категория ПО",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=self.software_type,
            name="Сервисное тестовое ПО",
            current_version="1.0",
        )

        # Карточка должна быть в работе.
        self.ticket = ChangeTicket.objects.create(
            software=self.software,
            created_by=self.executor,
            status=CardStatus.objects.get(
                code=CardStatusCode.IN_PROGRESS,
            ),
            source_version="1.0",
            target_version="2.0",
        )

        # Операция обновления.
        self.operation = Operation.objects.create(
            change_ticket=self.ticket,
            operation_type=OperationType.UPDATE,
            sequence_number=1,
            attempt_number=1,
            initiated_by=self.executor,
        )

        # Плановые шаги.
        self.backup_plan = PlanStep.objects.create(
            ticket=self.ticket,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name="Создать резервную копию",
            description="Создание тестовой резервной копии",
            planned_duration_min=10,
        )

        self.update_plan = PlanStep.objects.create(
            ticket=self.ticket,
            step_order=2,
            step_kind=StepKind.UPDATE,
            name="Обновить программное обеспечение",
            description="Установить новую версию",
            planned_duration_min=20,
        )

        # Фактические шаги.
        self.step1 = StepExecution.objects.create(
            operation=self.operation,
            plan_step=self.backup_plan,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name=self.backup_plan.name,
            description=self.backup_plan.description,
            planned_duration_min=10,
            status=StepExecutionStatus.AVAILABLE,
        )

        self.step2 = StepExecution.objects.create(
            operation=self.operation,
            plan_step=self.update_plan,
            step_order=2,
            step_kind=StepKind.UPDATE,
            name=self.update_plan.name,
            description=self.update_plan.description,
            planned_duration_min=20,
            status=StepExecutionStatus.LOCKED,
        )

    # --------------------------------------------------------
    # НАЧАЛО ВЫПОЛНЕНИЯ
    # --------------------------------------------------------

    def test_executor_can_start_available_step(self):
        step = start_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
        )

        self.assertEqual(
            step.status,
            StepExecutionStatus.IN_PROGRESS,
        )

        self.assertEqual(step.started_by, self.executor)
        self.assertIsNotNone(step.started_at)
        event = AuditEvent.objects.get(
            action=AuditAction.STEP_STARTED,
            object_id=self.step1.pk,
        )

        self.assertEqual(event.user, self.executor)
        self.assertEqual(
            event.object_type,
            "operations.stepexecution",
)

    def test_locked_step_cannot_be_started(self):
        with self.assertRaises(OperationServiceError):
            start_step(
                step_execution_id=self.step2.pk,
                actor=self.executor,
            )

    def test_approver_cannot_start_step(self):
        with self.assertRaises(PermissionDenied):
            start_step(
                step_execution_id=self.step1.pk,
                actor=self.approver,
            )

    # --------------------------------------------------------
    # ПОСЛЕДОВАТЕЛЬНОСТЬ ШАГОВ
    # --------------------------------------------------------

    def test_completing_step_unlocks_next_step(self):
        start_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
        )

        complete_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
            result="Резервная копия создана",
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.STEP_COMPLETED,
                object_id=self.step1.pk,
            ).exists()
        )

        self.step2.refresh_from_db()

        self.assertEqual(
            self.step2.status,
            StepExecutionStatus.AVAILABLE,
        )

    def test_next_step_cannot_start_before_previous_completed(self):
        # Искусственно открываем второй шаг,
        # чтобы проверить именно серверное ограничение.
        self.step2.status = StepExecutionStatus.AVAILABLE
        self.step2.save(update_fields=["status", "updated_at"])

        with self.assertRaises(OperationServiceError):
            start_step(
                step_execution_id=self.step2.pk,
                actor=self.executor,
            )

    def test_two_steps_cannot_run_simultaneously(self):
        # Первый шаг уже выполняется.
        self.step1.status = StepExecutionStatus.IN_PROGRESS
        self.step1.started_by = self.executor
        self.step1.started_at = timezone.now()

        self.step1.save(
            update_fields=[
                "status",
                "started_by",
                "started_at",
                "updated_at",
            ]
        )

        # Попытка начать второй шаг.
        self.step2.status = StepExecutionStatus.AVAILABLE
        self.step2.save(
            update_fields=["status", "updated_at"]
        )

        with self.assertRaises(OperationServiceError):
            start_step(
                step_execution_id=self.step2.pk,
                actor=self.executor,
            )

    # --------------------------------------------------------
    # УСПЕШНОЕ ЗАВЕРШЕНИЕ
    # --------------------------------------------------------

    def test_all_steps_complete_operation_and_ticket(self):
        # Первый шаг.
        start_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
        )

        complete_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
            result="Резервная копия создана",
        )

        # Второй шаг.
        start_step(
            step_execution_id=self.step2.pk,
            actor=self.executor,
        )

        complete_step(
            step_execution_id=self.step2.pk,
            actor=self.executor,
            result="Обновление успешно выполнено",
        )

        self.operation.refresh_from_db()
        self.ticket.refresh_from_db()

        self.assertEqual(
            self.operation.status,
            OperationStatus.COMPLETED,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action=AuditAction.STEP_COMPLETED,
            ).count(),
            2,
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.OPERATION_COMPLETED,
                object_id=self.operation.pk,
            ).exists()
        )

        self.assertIsNotNone(self.operation.finished_at)

        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.COMPLETED,
        )

    # --------------------------------------------------------
    # ОШИБКИ ВЫПОЛНЕНИЯ
    # --------------------------------------------------------

    def test_failed_step_terminates_operation(self):
        start_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
        )

        fail_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
            error_message="Ошибка создания резервной копии",
        )

        self.step1.refresh_from_db()
        self.operation.refresh_from_db()
        self.ticket.refresh_from_db()

        self.assertEqual(
            self.step1.status,
            StepExecutionStatus.FAILED,
        )

        self.assertEqual(
            self.operation.status,
            OperationStatus.FAILED,
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.STEP_FAILED,
                object_id=self.step1.pk,
            ).exists()
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.OPERATION_FAILED,
                object_id=self.operation.pk,
            ).exists()
        )

        self.assertIsNotNone(self.operation.finished_at)

        # Карточка остаётся в работе.
        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.IN_PROGRESS,
        )

    def test_failure_requires_error_message(self):
        start_step(
            step_execution_id=self.step1.pk,
            actor=self.executor,
        )

        with self.assertRaises(OperationServiceError):
            fail_step(
                step_execution_id=self.step1.pk,
                actor=self.executor,
                error_message="   ",
            )
class StartUpdateOperationTests(TestCase):

    def setUp(self):
        # Временная папка для тестовых пакетов.
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)

        self.media_override = override_settings(
            MEDIA_ROOT=self.temp_media.name
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        # Исполнитель.
        self.executor = User.objects.create_user(
            username="start_executor",
            email="start_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        # Руководитель, согласовавший карточку.
        self.approver = User.objects.create_user(
            username="start_approver",
            email="start_approver@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.approver,
            role=ModuleRole.APPROVER,
        )

        # Программное обеспечение.
        self.software_type = SoftwareType.objects.create(
            name="Категория для запуска обновления",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=self.software_type,
            name="Тестовое ПО для запуска",
            current_version="1.0",
        )

        # Тестовый пакет и его контрольная сумма.
        self.package_content = b"museum software update package"

        self.expected_hash = hashlib.sha256(
            self.package_content
        ).hexdigest()

        now = timezone.now()

        # Согласованная карточка.
        self.ticket = ChangeTicket.objects.create(
            software=self.software,
            created_by=self.executor,
            approved_by=self.approver,
            status=CardStatus.objects.get(
                code=CardStatusCode.APPROVED,
            ),
            source_version="1.0",
            target_version="2.0",
            expected_sha256=self.expected_hash,
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            resources_check_status=ResourcesCheckStatus.SUFFICIENT,
            window_start_at=now - timedelta(minutes=30),
            window_end_at=now + timedelta(minutes=30),
            package_file=SimpleUploadedFile(
                "update-package.bin",
                self.package_content,
                content_type="application/octet-stream",
            ),
        )

        # Первый шаг обязательно резервное копирование.
        self.backup_step = PlanStep.objects.create(
            ticket=self.ticket,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name="Создать резервную копию",
            description="Создание резервной копии ПО",
            planned_duration_min=10,
        )

        # Второй шаг — обновление.
        self.update_step = PlanStep.objects.create(
            ticket=self.ticket,
            step_order=2,
            step_kind=StepKind.UPDATE,
            name="Установить обновление",
            description="Установка новой версии",
            planned_duration_min=20,
        )

    # --------------------------------------------------------
    # УСПЕШНЫЙ ЗАПУСК
    # --------------------------------------------------------

    def test_successful_start_creates_operation(self):

        operation = start_update_operation(
            ticket_id=self.ticket.pk,
            actor=self.executor,
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.UPDATE_STARTED,
                object_id=operation.pk,
                user=self.executor,
            ).exists()
        )

        self.ticket.refresh_from_db()

        self.assertEqual(
            operation.operation_type,
            OperationType.UPDATE,
        )

        self.assertEqual(operation.sequence_number, 1)
        self.assertEqual(operation.attempt_number, 1)
        self.assertEqual(operation.status, OperationStatus.RUNNING)

        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.IN_PROGRESS,
        )

        self.assertEqual(
            self.ticket.hash_status,
            HashStatus.MATCH,
        )

        self.assertEqual(
            self.ticket.actual_sha256,
            self.expected_hash,
        )

        self.assertIsNotNone(self.ticket.hash_checked_at)

    # --------------------------------------------------------
    # СОЗДАНИЕ СНИМКОВ ПЛАНА
    # --------------------------------------------------------

    def test_start_creates_snapshots_of_all_plan_steps(self):

        operation = start_update_operation(
            ticket_id=self.ticket.pk,
            actor=self.executor,
        )

        executions = list(
            operation.steps.order_by("step_order")
        )

        self.assertEqual(len(executions), 2)

        self.assertEqual(
            executions[0].name,
            self.backup_step.name,
        )

        self.assertEqual(
            executions[1].name,
            self.update_step.name,
        )

        self.assertEqual(
            executions[0].status,
            StepExecutionStatus.AVAILABLE,
        )

        self.assertEqual(
            executions[1].status,
            StepExecutionStatus.LOCKED,
        )

    def test_snapshot_does_not_change_when_plan_is_edited(self):

        operation = start_update_operation(
            ticket_id=self.ticket.pk,
            actor=self.executor,
        )

        execution = operation.steps.get(step_order=2)

        original_name = execution.name

        self.update_step.name = "Новое название планового шага"
        self.update_step.save(update_fields=["name", "updated_at"])

        execution.refresh_from_db()

        self.assertEqual(execution.name, original_name)
        self.assertNotEqual(
            execution.name,
            self.update_step.name,
        )

    # --------------------------------------------------------
    # ПРОВЕРКА SHA-256
    # --------------------------------------------------------

    def test_hash_mismatch_does_not_create_operation(self):

        self.ticket.expected_sha256 = "0" * 64
        self.ticket.save(update_fields=["expected_sha256"])

        with self.assertRaises(OperationServiceError):
            start_update_operation(
                ticket_id=self.ticket.pk,
                actor=self.executor,
            )

        self.ticket.refresh_from_db()

        self.assertEqual(
            self.ticket.hash_status,
            HashStatus.MISMATCH,
        )

        self.assertEqual(
            self.ticket.actual_sha256,
            self.expected_hash,
        )

        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.APPROVED,
        )

        self.assertEqual(
            Operation.objects.filter(
                change_ticket=self.ticket
            ).count(),
            0,
        )

    # --------------------------------------------------------
    # ПРОВЕРКА СОГЛАСОВАНИЯ И ВРЕМЕННОГО ОКНА
    # --------------------------------------------------------

    def test_operation_cannot_start_without_approval(self):

        self.ticket.approved_by = None
        self.ticket.save(update_fields=["approved_by"])

        with self.assertRaises(OperationServiceError):
            start_update_operation(
                ticket_id=self.ticket.pk,
                actor=self.executor,
            )

        self.assertFalse(
            Operation.objects.filter(
                change_ticket=self.ticket
            ).exists()
        )

    def test_operation_cannot_start_outside_approved_window(self):

        now = timezone.now()

        self.ticket.window_start_at = now - timedelta(hours=2)
        self.ticket.window_end_at = now - timedelta(hours=1)

        self.ticket.save(
            update_fields=[
                "window_start_at",
                "window_end_at",
            ]
        )

        with self.assertRaises(OperationServiceError):
            start_update_operation(
                ticket_id=self.ticket.pk,
                actor=self.executor,
            )

        self.assertFalse(
            Operation.objects.filter(
                change_ticket=self.ticket
            ).exists()
        )

    # --------------------------------------------------------
    # ПРОВЕРКА ПРАВ ИСПОЛНИТЕЛЯ
    # --------------------------------------------------------

    def test_another_executor_cannot_start_ticket(self):

        another_executor = User.objects.create_user(
            username="another_executor",
            email="another_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=another_executor,
            role=ModuleRole.EXECUTOR,
        )

        with self.assertRaises(PermissionDenied):
            start_update_operation(
                ticket_id=self.ticket.pk,
                actor=another_executor,
            )

        self.assertFalse(
            Operation.objects.filter(
                change_ticket=self.ticket
            ).exists()
        )

    # --------------------------------------------------------
    # ПОВТОРНАЯ ПОПЫТКА
    # --------------------------------------------------------

    def test_failed_update_can_be_retried(self):

        self.ticket.status = CardStatus.objects.get(
            code=CardStatusCode.IN_PROGRESS,
        )

        self.ticket.save(update_fields=["status"])

        previous_operation = Operation.objects.create(
            change_ticket=self.ticket,
            operation_type=OperationType.UPDATE,
            sequence_number=1,
            attempt_number=1,
            initiated_by=self.executor,
            status=OperationStatus.FAILED,
            finished_at=timezone.now(),
            error_message="Предыдущая попытка завершилась ошибкой",
        )

        operation = start_update_operation(
            ticket_id=self.ticket.pk,
            actor=self.executor,
        )

        self.assertEqual(
            previous_operation.status,
            OperationStatus.FAILED,
        )

        self.assertEqual(operation.sequence_number, 2)
        self.assertEqual(operation.attempt_number, 2)
        self.assertEqual(operation.status, OperationStatus.RUNNING)
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.UPDATE_STARTED,
                object_id=operation.pk,
                user=self.executor,
            ).exists()
        )