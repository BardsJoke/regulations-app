
import hashlib
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment
from backups.models import Backup, BackupIntegrityStatus
from catalogs.models import (
    CardStatus,
    CardStatusCode,
    MuseumSoftware,
    RollbackReason,
    SoftwareType,
)
from changes.models import ChangeTicket, StepKind

from .models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
    StepTemplate,
)
from .services import (
    OperationServiceError,
    complete_step,
    start_rollback_operation,
    start_step,
)


User = get_user_model()


class StartRollbackOperationTests(TestCase):
    """
    Проверка запуска операций отката.

    Проверяются:
    - права доступа;
    - исходная операция обновления;
    - резервная копия;
    - причина отката;
    - шаблоны шагов;
    - нумерация операций;
    - создание снимков;
    - переходы статусов;
    - завершение восстановления.
    """

    def setUp(self):
        # ----------------------------------------------------
        # ВРЕМЕННОЕ ФАЙЛОВОЕ ХРАНИЛИЩЕ
        # ----------------------------------------------------

        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)

        self.media_override = override_settings(
            MEDIA_ROOT=self.temp_media.name
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        # ----------------------------------------------------
        # ПОЛЬЗОВАТЕЛИ
        # ----------------------------------------------------

        self.executor = User.objects.create_user(
            username="rollback_executor",
            email="rollback_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        self.approver = User.objects.create_user(
            username="rollback_approver",
            email="rollback_approver@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.approver,
            role=ModuleRole.APPROVER,
        )

        # ----------------------------------------------------
        # ПРОГРАММНОЕ ОБЕСПЕЧЕНИЕ
        # ----------------------------------------------------

        self.software_type = SoftwareType.objects.create(
            name="Категория тестирования отката",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=self.software_type,
            name="Тестовое ПО для отката",
            current_version="2.0",
        )

        # ----------------------------------------------------
        # КАРТОЧКА ИСХОДНОГО ИЗМЕНЕНИЯ
        # ----------------------------------------------------

        self.ticket = self.create_ticket()

        # ----------------------------------------------------
        # ПРИЧИНА ОТКАТА
        # ----------------------------------------------------

        self.rollback_reason = RollbackReason.objects.create(
            name="Ошибка после обновления",
            is_active=True,
        )

        # ----------------------------------------------------
        # ИСХОДНАЯ ОПЕРАЦИЯ UPDATE
        # ----------------------------------------------------

        self.parent_operation = self.create_update_operation()

        # ----------------------------------------------------
        # СОДЕРЖИМОЕ РЕЗЕРВНОЙ КОПИИ
        # ----------------------------------------------------

        self.backup_content = (
            b"test museum system backup content"
        )

        self.backup = self.create_backup()

        # ----------------------------------------------------
        # ШАБЛОНЫ ШАГОВ ОТКАТА
        # ----------------------------------------------------

        self.restore_template = StepTemplate.objects.create(
            operation_type=OperationType.ROLLBACK,
            step_kind=StepKind.OTHER,
            name="Восстановить резервную копию",
            description="Восстановление исходного состояния системы",
            default_duration_min=30,
            sort_order=1,
            is_active=True,
        )

        self.verify_template = StepTemplate.objects.create(
            operation_type=OperationType.ROLLBACK,
            step_kind=StepKind.VERIFICATION,
            name="Проверить восстановленную систему",
            description="Контроль работоспособности системы",
            default_duration_min=15,
            sort_order=2,
            is_active=True,
        )

    # ========================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ========================================================

    def create_ticket(self, status_code=CardStatusCode.IN_PROGRESS):
        return ChangeTicket.objects.create(
            software=self.software,
            created_by=self.executor,
            status=CardStatus.objects.get(code=status_code),
            source_version="1.0",
            target_version="2.0",
        )

    def create_update_operation(
        self,
        *,
        ticket=None,
        sequence_number=1,
        attempt_number=1,
        status=OperationStatus.FAILED,
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
            initiated_by=self.executor,
            status=status,
            finished_at=finished_at,
            error_message=(
                "Тестовая ошибка обновления"
                if status == OperationStatus.FAILED
                else ""
            ),
        )

    def create_backup(
        self,
        *,
        operation=None,
        content=None,
        integrity_status=BackupIntegrityStatus.VALID,
    ):
        operation = operation or self.parent_operation
        content = content or self.backup_content

        verified_at = (
            timezone.now()
            if integrity_status == BackupIntegrityStatus.VALID
            else None
        )

        backup = Backup(
            operation=operation,
            file=SimpleUploadedFile(
                "test-backup.bin",
                content,
                content_type="application/octet-stream",
            ),
            file_sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            created_by=self.executor,
            integrity_status=integrity_status,
            verified_at=verified_at,
            verification_error="",
        )

        backup.full_clean()
        backup.save()

        return backup

    def create_previous_rollback(
        self,
        *,
        status=OperationStatus.FAILED,
        sequence_number=2,
        attempt_number=1,
    ):
        finished_at = (
            None
            if status == OperationStatus.RUNNING
            else timezone.now()
        )

        return Operation.objects.create(
            change_ticket=self.ticket,
            operation_type=OperationType.ROLLBACK,
            sequence_number=sequence_number,
            attempt_number=attempt_number,
            parent_operation=self.parent_operation,
            backup_used=self.backup,
            rollback_reason=self.rollback_reason,
            initiated_by=self.executor,
            status=status,
            finished_at=finished_at,
            error_message=(
                "Предыдущий откат завершился ошибкой"
                if status == OperationStatus.FAILED
                else ""
            ),
        )

    def start_rollback(
        self,
        *,
        parent_operation=None,
        backup=None,
        rollback_reason=None,
        actor=None,
    ):
        return start_rollback_operation(
            parent_operation_id=(
                parent_operation or self.parent_operation
            ).pk,
            backup_id=(
                backup or self.backup
            ).pk,
            rollback_reason_id=(
                rollback_reason or self.rollback_reason
            ).pk,
            actor=actor or self.executor,
        )

    # ========================================================
    # УСПЕШНЫЙ ЗАПУСК ОТКАТА
    # ========================================================

    def test_successful_start_creates_rollback_operation(self):

        operation = self.start_rollback()

        self.assertEqual(
            operation.operation_type,
            OperationType.ROLLBACK,
        )

        self.assertEqual(
            operation.status,
            OperationStatus.RUNNING,
        )

        self.assertEqual(
            operation.parent_operation_id,
            self.parent_operation.pk,
        )

        self.assertEqual(
            operation.backup_used_id,
            self.backup.pk,
        )

        self.assertEqual(
            operation.rollback_reason_id,
            self.rollback_reason.pk,
        )

        self.assertEqual(
            operation.initiated_by,
            self.executor,
        )

        self.assertEqual(operation.sequence_number, 2)
        self.assertEqual(operation.attempt_number, 1)

    # ========================================================
    # СОЗДАНИЕ СОБСТВЕННЫХ ШАГОВ ОТКАТА
    # ========================================================

    def test_start_creates_snapshots_from_rollback_templates(self):

        # Дополнительный шаблон UPDATE не должен использоваться.
        StepTemplate.objects.create(
            operation_type=OperationType.UPDATE,
            step_kind=StepKind.OTHER,
            name="Шаблон только для обновления",
            default_duration_min=10,
            sort_order=1,
        )

        # Неактивный шаблон отката тоже не должен использоваться.
        StepTemplate.objects.create(
            operation_type=OperationType.ROLLBACK,
            step_kind=StepKind.OTHER,
            name="Неактивный шаг отката",
            default_duration_min=10,
            sort_order=3,
            is_active=False,
        )

        operation = self.start_rollback()

        executions = list(
            operation.steps.order_by("step_order")
        )

        self.assertEqual(len(executions), 2)

        self.assertEqual(
            executions[0].name,
            self.restore_template.name,
        )

        self.assertEqual(
            executions[1].name,
            self.verify_template.name,
        )

        self.assertEqual(
            executions[0].step_order,
            1,
        )

        self.assertEqual(
            executions[1].step_order,
            2,
        )

        self.assertIsNone(executions[0].plan_step_id)
        self.assertIsNone(executions[1].plan_step_id)

        self.assertEqual(
            executions[0].status,
            StepExecutionStatus.AVAILABLE,
        )

        self.assertEqual(
            executions[1].status,
            StepExecutionStatus.LOCKED,
        )

        self.assertEqual(
            executions[0].planned_duration_min,
            self.restore_template.default_duration_min,
        )

    # ========================================================
    # ПРОВЕРКА РЕЗЕРВНОЙ КОПИИ
    # ========================================================

    def test_backup_is_reverified_before_rollback(self):

        backup = self.create_backup(
            integrity_status=BackupIntegrityStatus.NOT_CHECKED,
        )

        operation = self.start_rollback(
            backup=backup,
        )

        backup.refresh_from_db()

        self.assertEqual(
            backup.integrity_status,
            BackupIntegrityStatus.VALID,
        )

        self.assertIsNotNone(backup.verified_at)
        self.assertTrue(backup.is_usable)

        self.assertEqual(
            operation.backup_used_id,
            backup.pk,
        )

    def test_corrupted_backup_is_rejected_and_invalid_status_is_saved(self):

        # Повреждаем файл после создания записи.
        corrupted_content = (
            b"X" + self.backup_content[1:]
        )

        with open(self.backup.file.path, "wb") as stored_file:
            stored_file.write(corrupted_content)

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

        self.backup.refresh_from_db()

        self.assertEqual(
            self.backup.integrity_status,
            BackupIntegrityStatus.INVALID,
        )

        self.assertIsNotNone(self.backup.verified_at)

        self.assertNotEqual(
            self.backup.verification_error,
            "",
        )

        self.assertFalse(self.backup.is_usable)

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    def test_missing_backup_file_is_rejected_and_error_status_is_saved(self):

        self.backup.file.storage.delete(
            self.backup.file.name
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

        self.backup.refresh_from_db()

        self.assertEqual(
            self.backup.integrity_status,
            BackupIntegrityStatus.ERROR,
        )

        self.assertIsNotNone(self.backup.verified_at)

        self.assertNotEqual(
            self.backup.verification_error,
            "",
        )

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    def test_backup_from_another_operation_cannot_be_used(self):

        other_ticket = self.create_ticket()

        other_update = self.create_update_operation(
            ticket=other_ticket,
            sequence_number=1,
            attempt_number=1,
        )

        other_backup = self.create_backup(
            operation=other_update,
            content=b"another operation backup",
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback(
                backup=other_backup,
            )

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    # ========================================================
    # ПРОВЕРКА ИСХОДНОЙ ОПЕРАЦИИ
    # ========================================================

    def test_rollback_cannot_be_started_from_running_update(self):

        self.parent_operation.status = OperationStatus.RUNNING
        self.parent_operation.finished_at = None

        self.parent_operation.save(
            update_fields=[
                "status",
                "finished_at",
                "updated_at",
            ]
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

    def test_rollback_cannot_reference_another_rollback(self):

        previous_rollback = self.create_previous_rollback()

        with self.assertRaises(OperationServiceError):
            self.start_rollback(
                parent_operation=previous_rollback,
            )

    def test_only_latest_update_can_be_rolled_back(self):

        self.create_update_operation(
            sequence_number=2,
            attempt_number=2,
            status=OperationStatus.FAILED,
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    # ========================================================
    # ПРИЧИНА ОТКАТА
    # ========================================================

    def test_inactive_rollback_reason_cannot_be_used(self):

        self.rollback_reason.is_active = False

        self.rollback_reason.save(
            update_fields=["is_active", "updated_at"]
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    # ========================================================
    # ПРАВА ДОСТУПА
    # ========================================================

    def test_approver_cannot_start_rollback(self):

        with self.assertRaises(PermissionDenied):
            self.start_rollback(
                actor=self.approver,
            )

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    def test_another_executor_cannot_start_rollback(self):

        another_executor = User.objects.create_user(
            username="another_rollback_executor",
            email="another_rollback_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=another_executor,
            role=ModuleRole.EXECUTOR,
        )

        with self.assertRaises(PermissionDenied):
            self.start_rollback(
                actor=another_executor,
            )

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    # ========================================================
    # ПРОВЕРКА ШАБЛОНОВ
    # ========================================================

    def test_rollback_cannot_start_without_active_templates(self):

        StepTemplate.objects.filter(
            operation_type=OperationType.ROLLBACK,
        ).update(is_active=False)

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

        self.assertFalse(
            Operation.objects.filter(
                operation_type=OperationType.ROLLBACK,
            ).exists()
        )

    # ========================================================
    # ЗАЩИТА ОТ ПОВТОРНОГО ЗАПУСКА
    # ========================================================

    def test_rollback_cannot_start_while_another_operation_is_running(self):

        self.create_previous_rollback(
            status=OperationStatus.RUNNING,
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

    def test_successfully_completed_rollback_cannot_be_repeated(self):

        self.create_previous_rollback(
            status=OperationStatus.COMPLETED,
        )

        with self.assertRaises(OperationServiceError):
            self.start_rollback()

    def test_failed_rollback_can_be_retried_with_new_numbers(self):

        self.create_previous_rollback(
            status=OperationStatus.FAILED,
            sequence_number=2,
            attempt_number=1,
        )

        operation = self.start_rollback()

        self.assertEqual(
            operation.sequence_number,
            3,
        )

        self.assertEqual(
            operation.attempt_number,
            2,
        )

        self.assertEqual(
            operation.status,
            OperationStatus.RUNNING,
        )

    # ========================================================
    # ОБНОВЛЕНИЕ СТАТУСА КАРТОЧКИ
    # ========================================================

    def test_rollback_of_completed_update_returns_ticket_to_progress(self):

        self.parent_operation.status = OperationStatus.COMPLETED
        self.parent_operation.finished_at = timezone.now()

        self.parent_operation.save(
            update_fields=[
                "status",
                "finished_at",
                "updated_at",
            ]
        )

        self.ticket.status = CardStatus.objects.get(
            code=CardStatusCode.COMPLETED,
        )

        self.ticket.save(
            update_fields=["status", "updated_at"]
        )

        self.start_rollback()

        self.ticket.refresh_from_db()

        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.IN_PROGRESS,
        )

    # ========================================================
    # ПОЛНЫЙ ЦИКЛ ВЫПОЛНЕНИЯ ОТКАТА
    # ========================================================

    def test_completing_all_rollback_steps_marks_ticket_as_rolled_back(self):

        operation = self.start_rollback()

        steps = list(
            operation.steps.order_by("step_order")
        )

        self.assertEqual(len(steps), 2)

        # Выполняем шаги последовательно.
        for step in steps:

            started_step = start_step(
                step_execution_id=step.pk,
                actor=self.executor,
            )

            self.assertEqual(
                started_step.status,
                StepExecutionStatus.IN_PROGRESS,
            )

            complete_step(
                step_execution_id=step.pk,
                actor=self.executor,
                result=f"Шаг {step.step_order} успешно выполнен.",
            )

        # Проверяем завершение операции.
        operation.refresh_from_db()
        self.ticket.refresh_from_db()

        self.assertEqual(
            operation.status,
            OperationStatus.COMPLETED,
        )

        self.assertIsNotNone(operation.finished_at)

        self.assertEqual(
            self.ticket.status.code,
            CardStatusCode.ROLLED_BACK,
        )

        self.assertTrue(
            operation.steps.filter(
                status=StepExecutionStatus.COMPLETED,
            ).count() == 2
        )
