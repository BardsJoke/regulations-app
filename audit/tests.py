
import uuid
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from .models import AuditEvent


User = get_user_model()


class AuditEventTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username="audit_executor",
            email="audit_executor@example.test",
            password="test-password",
        )

        self.object_id = uuid.uuid4()

    # ========================================================
    # ВСПОМОГАТЕЛЬНЫЙ МЕТОД
    # ========================================================

    def create_event(self, **kwargs):
        """
        Создаёт стандартное тестовое событие аудита.
        """

        data = {
            "user": self.user,
            "action": "operation.update.started",
            "object_type": "operations.operation",
            "object_id": self.object_id,
            "details": {
                "status": "running",
            },
            "ip_address": "192.0.2.10",
        }

        data.update(kwargs)

        return AuditEvent.record(**data)

    # ========================================================
    # СОЗДАНИЕ СОБЫТИЙ
    # ========================================================

    def test_audit_event_can_be_created(self):

        event = self.create_event()

        self.assertIsNotNone(event.pk)

        self.assertIsInstance(event.pk, int)

        self.assertEqual(
            event.user,
            self.user,
        )

        self.assertEqual(
            event.action,
            "operation.update.started",
        )

        self.assertEqual(
            event.object_type,
            "operations.operation",
        )

        self.assertEqual(
            event.object_id,
            self.object_id,
        )

        self.assertEqual(
            event.details,
            {"status": "running"},
        )

        self.assertEqual(
            event.ip_address,
            "192.0.2.10",
        )

        self.assertIsNotNone(event.occurred_at)

    def test_audit_event_uses_current_username_as_snapshot(self):

        event = self.create_event()

        self.assertEqual(
            event.actor_name_snapshot,
            self.user.get_full_name().strip()
            or self.user.get_username(),
        )

    def test_audit_event_can_be_created_from_object(self):

        dummy_object = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="operations",
                model_name="operation",
            ),
            pk=self.object_id,
        )

        event = AuditEvent.record(
            user=self.user,
            action="operation.rollback.started",
            obj=dummy_object,
        )

        self.assertEqual(
            event.object_type,
            "operations.operation",
        )

        self.assertEqual(
            event.object_id,
            self.object_id,
        )

    # ========================================================
    # СНИМОК ИМЕНИ ПОЛЬЗОВАТЕЛЯ
    # ========================================================

    def test_actor_name_snapshot_does_not_change(self):

        event = self.create_event()

        original_snapshot = event.actor_name_snapshot

        self.user.username = "renamed_executor"

        self.user.save(
            update_fields=["username"],
        )

        event.refresh_from_db()

        self.assertEqual(
            event.actor_name_snapshot,
            original_snapshot,
        )

    # ========================================================
    # ЗАПРЕТ ИЗМЕНЕНИЯ ЗАПИСЕЙ
    # ========================================================

    def test_existing_event_cannot_be_updated(self):

        event = self.create_event()

        event.action = "operation.rollback.started"

        with self.assertRaises(ValidationError):
            event.save()

        event.refresh_from_db()

        self.assertEqual(
            event.action,
            "operation.update.started",
        )

    def test_existing_event_cannot_be_deleted(self):

        event = self.create_event()

        with self.assertRaises(ValidationError):
            event.delete()

        self.assertTrue(
            AuditEvent.objects.filter(
                pk=event.pk,
            ).exists()
        )

    def test_queryset_update_is_forbidden(self):

        event = self.create_event()

        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(
                pk=event.pk,
            ).update(
                action="modified",
            )

        event.refresh_from_db()

        self.assertEqual(
            event.action,
            "operation.update.started",
        )

    def test_queryset_delete_is_forbidden(self):

        event = self.create_event()

        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(
                pk=event.pk,
            ).delete()

        self.assertTrue(
            AuditEvent.objects.filter(
                pk=event.pk,
            ).exists()
        )

    def test_bulk_update_is_forbidden(self):

        event = self.create_event()

        event.action = "modified"

        with self.assertRaises(ValidationError):
            AuditEvent.objects.bulk_update(
                [event],
                ["action"],
            )

        event.refresh_from_db()

        self.assertEqual(
            event.action,
            "operation.update.started",
        )

    # ========================================================
    # МАССОВОЕ СОЗДАНИЕ
    # ========================================================

    def test_bulk_create_of_new_events_is_allowed(self):

        event = AuditEvent(
            user=self.user,
            action="backup.created",
            object_type="backups.backup",
            object_id=uuid.uuid4(),
            details={"integrity": "not_checked"},
        )

        created = AuditEvent.objects.bulk_create(
            [event],
        )

        self.assertEqual(
            len(created),
            1,
        )

        saved_event = AuditEvent.objects.get(
            pk=event.pk,
        )

        self.assertEqual(
            saved_event.actor_name_snapshot,
            self.user.get_full_name().strip()
            or self.user.get_username(),
        )

    # ========================================================
    # ПРОВЕРКА ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ
    # ========================================================

    def test_event_requires_user(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                user=None,
            )

    def test_event_requires_action(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                action="",
            )

    def test_event_requires_object_type(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                object_type="",
            )

    def test_event_requires_object_identifier(self):

        with self.assertRaises(ValidationError):
            AuditEvent.record(
                user=self.user,
                action="operation.update.started",
                object_type="operations.operation",
            )

    def test_event_requires_valid_uuid(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                object_id="not-a-valid-uuid",
            )

    # ========================================================
    # ПРОВЕРКА JSON
    # ========================================================

    def test_details_must_be_a_dictionary(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                details=["invalid", "structure"],
            )

    def test_details_must_contain_json_serializable_values(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                details={
                    "invalid_value": object(),
                },
            )

    def test_empty_details_are_allowed(self):

        event = self.create_event(
            details={},
        )

        self.assertEqual(
            event.details,
            {},
        )

    # ========================================================
    # ПРОВЕРКА IP-АДРЕСА
    # ========================================================

    def test_valid_ipv4_address_is_allowed(self):

        event = self.create_event(
            ip_address="192.0.2.15",
        )

        self.assertEqual(
            event.ip_address,
            "192.0.2.15",
        )

    def test_valid_ipv6_address_is_allowed(self):

        event = self.create_event(
            ip_address="2001:db8::1",
        )

        self.assertEqual(
            event.ip_address,
            "2001:db8::1",
        )

    def test_invalid_ip_address_is_rejected(self):

        with self.assertRaises(ValidationError):
            self.create_event(
                ip_address="999.999.999.999",
            )

    def test_ip_address_can_be_empty(self):

        event = self.create_event(
            ip_address=None,
        )

        self.assertIsNone(
            event.ip_address,
        )

    # ========================================================
    # ЗАЩИТА СВЯЗИ С ПОЛЬЗОВАТЕЛЕМ
    # ========================================================

    def test_user_cannot_be_deleted_while_audit_events_exist(self):

        self.create_event()

        with self.assertRaises(ProtectedError):
            self.user.delete()

        self.assertTrue(
            User.objects.filter(
                pk=self.user.pk,
            ).exists()
        )
