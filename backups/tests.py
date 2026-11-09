import hashlib
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from access.models import ModuleRole, RoleAssignment

from catalogs.models import (
    CardStatus,
    CardStatusCode,
    MuseumSoftware,
    SoftwareType,
)

from changes.models import ChangeTicket, PlanStep, StepKind

from operations.models import (
    Operation,
    OperationStatus,
    OperationType,
    StepExecution,
    StepExecutionStatus,
)

from .models import Backup, BackupIntegrityStatus
from .services import (
    BackupServiceError,
    create_backup,
    verify_backup,
    get_usable_backups,
)


User = get_user_model()


class BackupServiceTests(TestCase):

    def setUp(self):
        # Временное файловое хранилище.
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)

        self.media_override = override_settings(
            MEDIA_ROOT=self.temp_media.name
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        # Исполнитель.
        self.executor = User.objects.create_user(
            username="backup_executor",
            email="backup_executor@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.executor,
            role=ModuleRole.EXECUTOR,
        )

        # Руководитель для проверки прав доступа.
        self.approver = User.objects.create_user(
            username="backup_approver",
            email="backup_approver@example.test",
            password="test-password",
        )

        RoleAssignment.objects.create(
            user=self.approver,
            role=ModuleRole.APPROVER,
        )

        # Программное обеспечение.
        self.software_type = SoftwareType.objects.create(
            name="Категория тестовых резервных копий",
        )

        self.software = MuseumSoftware.objects.create(
            software_type=self.software_type,
            name="Тестовое ПО для резервного копирования",
            current_version="1.0",
        )

        # Карточка изменения.
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
            status=OperationStatus.RUNNING,
        )

        # Первый плановый шаг — резервное копирование.
        self.plan_step = PlanStep.objects.create(
            ticket=self.ticket,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name="Создать резервную копию",
            description="Создание резервной копии системы",
            planned_duration_min=10,
        )

        # Фактический шаг уже выполняется.
        self.step = StepExecution.objects.create(
            operation=self.operation,
            plan_step=self.plan_step,
            step_order=1,
            step_kind=StepKind.BACKUP,
            name=self.plan_step.name,
            description=self.plan_step.description,
            planned_duration_min=10,
            status=StepExecutionStatus.IN_PROGRESS,
            started_by=self.executor,
            started_at=timezone.now(),
        )

        self.file_content = b"museum backup test content"

    def create_test_upload(self):
        return SimpleUploadedFile(
            "backup.zip",
            self.file_content,
            content_type="application/zip",
        )

    def create_test_backup(self):
        return create_backup(
            operation_id=self.operation.pk,
            actor=self.executor,
            file=self.create_test_upload(),
        )

    # ========================================================
    # СОЗДАНИЕ РЕЗЕРВНОЙ КОПИИ
    # ========================================================

    def test_backup_is_created_with_correct_metadata(self):

        backup = self.create_test_backup()

        expected_hash = hashlib.sha256(
            self.file_content
        ).hexdigest()

        self.assertEqual(
            backup.file_sha256,
            expected_hash,
        )

        self.assertEqual(
            backup.size_bytes,
            len(self.file_content),
        )

        self.assertEqual(
            backup.integrity_status,
            BackupIntegrityStatus.NOT_CHECKED,
        )

        self.assertEqual(
            backup.created_by,
            self.executor,
        )

        self.assertTrue(
            backup.file.storage.exists(backup.file.name)
        )

    def test_empty_backup_cannot_be_created(self):

        empty_file = SimpleUploadedFile(
            "empty.zip",
            b"",
            content_type="application/zip",
        )

        with self.assertRaises(BackupServiceError):
            create_backup(
                operation_id=self.operation.pk,
                actor=self.executor,
                file=empty_file,
            )

        self.assertEqual(Backup.objects.count(), 0)

    def test_backup_cannot_be_created_outside_backup_step(self):

        self.step.status = StepExecutionStatus.AVAILABLE
        self.step.started_by = None
        self.step.started_at = None

        self.step.save(
            update_fields=[
                "status",
                "started_by",
                "started_at",
                "updated_at",
            ]
        )

        with self.assertRaises(BackupServiceError):
            self.create_test_backup()

        self.assertEqual(Backup.objects.count(), 0)

    # ========================================================
    # ПРАВА ДОСТУПА
    # ========================================================

    def test_approver_cannot_create_backup(self):

        with self.assertRaises(PermissionDenied):
            create_backup(
                operation_id=self.operation.pk,
                actor=self.approver,
                file=self.create_test_upload(),
            )

        self.assertEqual(Backup.objects.count(), 0)

    def test_approver_cannot_verify_backup(self):

        backup = self.create_test_backup()

        with self.assertRaises(PermissionDenied):
            verify_backup(
                backup_id=backup.pk,
                actor=self.approver,
            )

        backup.refresh_from_db()

        self.assertEqual(
            backup.integrity_status,
            BackupIntegrityStatus.NOT_CHECKED,
        )

    # ========================================================
    # ПРОВЕРКА ЦЕЛОСТНОСТИ
    # ========================================================

    def test_valid_backup_receives_valid_status(self):

        backup = self.create_test_backup()

        verified_backup = verify_backup(
            backup_id=backup.pk,
            actor=self.executor,
        )

        self.assertEqual(
            verified_backup.integrity_status,
            BackupIntegrityStatus.VALID,
        )

        self.assertIsNotNone(
            verified_backup.verified_at
        )

        self.assertEqual(
            verified_backup.verification_error,
            "",
        )

        self.assertTrue(
            verified_backup.is_usable
        )

    def test_corrupted_backup_receives_invalid_status(self):

        backup = self.create_test_backup()

        # Изменяем содержимое сохранённого файла.
        # Длина остаётся прежней, но SHA-256 меняется.
        corrupted_content = bytearray(self.file_content)

        corrupted_content[0] = (
            (corrupted_content[0] + 1) % 256
        )

        with open(backup.file.path, "wb") as stored_file:
            stored_file.write(corrupted_content)

        verified_backup = verify_backup(
            backup_id=backup.pk,
            actor=self.executor,
        )

        self.assertEqual(
            verified_backup.integrity_status,
            BackupIntegrityStatus.INVALID,
        )

        self.assertIsNotNone(
            verified_backup.verified_at
        )

        self.assertNotEqual(
            verified_backup.verification_error,
            "",
        )

        self.assertFalse(
            verified_backup.is_usable
        )

    def test_missing_backup_file_receives_error_status(self):

        backup = self.create_test_backup()

        # Удаляем файл непосредственно из хранилища,
        # не удаляя запись в базе данных.
        backup.file.storage.delete(backup.file.name)

        verified_backup = verify_backup(
            backup_id=backup.pk,
            actor=self.executor,
        )

        self.assertEqual(
            verified_backup.integrity_status,
            BackupIntegrityStatus.ERROR,
        )

        self.assertIsNotNone(
            verified_backup.verified_at
        )

        self.assertNotEqual(
            verified_backup.verification_error,
            "",
        )

        self.assertFalse(
            verified_backup.is_usable
        )

    # ========================================================
    # ВЫБОР РЕЗЕРВНЫХ КОПИЙ ДЛЯ ОТКАТА
    # ========================================================

    def test_only_valid_backups_are_returned(self):

        backup = self.create_test_backup()

        # Непроверенная копия не должна попадать в результат.
        usable = get_usable_backups(
            operation_id=self.operation.pk,
            actor=self.executor,
        )

        self.assertEqual(usable.count(), 0)

        # После успешной проверки копия становится пригодной.
        verify_backup(
            backup_id=backup.pk,
            actor=self.executor,
        )

        usable = get_usable_backups(
            operation_id=self.operation.pk,
            actor=self.executor,
        )

        self.assertEqual(usable.count(), 1)
        self.assertEqual(usable.first().pk, backup.pk)