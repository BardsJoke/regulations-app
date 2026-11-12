
import uuid
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import RequestFactory, TestCase

from .models import AuditEvent
from .services import (
    AuditAction,
    AuditServiceError,
    get_request_ip,
    record_event,
)


User = get_user_model()


class AuditServiceTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username="audit_service_executor",
            email="audit_service@example.test",
            password="test-password",
        )

        self.object_id = uuid.uuid4()

        self.factory = RequestFactory()

    # ========================================================
    # ВСПОМОГАТЕЛЬНЫЙ МЕТОД
    # ========================================================

    def create_dummy_object(self):
        """
        Создаёт тестовый объект с UUID и метаданными Django.
        """

        return SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="operations",
                model_name="operation",
            ),
            pk=self.object_id,
        )

    def record_test_event(self, **kwargs):
        data = {
            "actor": self.user,
            "action": AuditAction.UPDATE_STARTED,
            "obj": self.create_dummy_object(),
            "details": {
                "status": "running",
            },
        }

        data.update(kwargs)

        return record_event(**data)

    # ========================================================
    # РЕГИСТРАЦИЯ СОБЫТИЙ
    # ========================================================

    def test_event_can_be_registered_through_object(self):

        event = self.record_test_event()

        self.assertIsNotNone(event.pk)

        self.assertEqual(
            event.user,
            self.user,
        )

        self.assertEqual(
            event.action,
            AuditAction.UPDATE_STARTED,
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

    def test_event_can_be_registered_without_object(self):

        event = record_event(
            actor=self.user,
            action=AuditAction.BACKUP_CREATED,
            object_type="backups.backup",
            object_id=self.object_id,
        )

        self.assertEqual(
            event.object_type,
            "backups.backup",
        )

        self.assertEqual(
            event.object_id,
            self.object_id,
        )

    def test_explicit_object_type_overrides_model_type(self):

        event = record_event(
            actor=self.user,
            action=AuditAction.UPDATE_STARTED,
            obj=self.create_dummy_object(),
            object_type="custom.operation",
        )

        self.assertEqual(
            event.object_type,
            "custom.operation",
        )

        self.assertEqual(
            event.object_id,
            self.object_id,
        )

    def test_event_without_details_uses_empty_dictionary(self):

        event = self.record_test_event(
            details=None,
        )

        self.assertEqual(
            event.details,
            {},
        )

    def test_custom_action_can_be_registered(self):

        event = self.record_test_event(
            action="custom.test_event",
        )

        self.assertEqual(
            event.action,
            "custom.test_event",
        )

    # ========================================================
    # ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ
    # ========================================================

    def test_event_requires_actor(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                actor=None,
            )

    def test_event_requires_persisted_actor(self):

        unsaved_user = User(
            username="unsaved_executor",
        )

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                actor=unsaved_user,
            )

    # ========================================================
    # ПРОВЕРКА ДЕЙСТВИЯ
    # ========================================================

    def test_event_requires_action(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                action="",
            )

    def test_event_requires_string_action(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                action=123,
            )

    # ========================================================
    # ПРОВЕРКА ОБЪЕКТА
    # ========================================================

    def test_event_rejects_missing_object_information(self):

        with self.assertRaises(AuditServiceError):
            record_event(
                actor=self.user,
                action=AuditAction.UPDATE_STARTED,
            )

    def test_event_rejects_unsaved_object(self):

        unsaved_object = SimpleNamespace(
            pk=None,
            _meta=SimpleNamespace(
                app_label="operations",
                model_name="operation",
            ),
        )

        with self.assertRaises(AuditServiceError):
            record_event(
                actor=self.user,
                action=AuditAction.UPDATE_STARTED,
                obj=unsaved_object,
            )

    def test_event_rejects_invalid_object_identifier(self):

        with self.assertRaises(AuditServiceError):
            record_event(
                actor=self.user,
                action=AuditAction.UPDATE_STARTED,
                object_type="operations.operation",
                object_id="invalid-uuid",
            )

    def test_event_rejects_empty_object_type(self):

        with self.assertRaises(AuditServiceError):
            record_event(
                actor=self.user,
                action=AuditAction.UPDATE_STARTED,
                object_type="   ",
                object_id=self.object_id,
            )

    # ========================================================
    # ПРОВЕРКА JSON
    # ========================================================

    def test_event_rejects_non_dictionary_details(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                details=["invalid", "data"],
            )

    def test_event_rejects_non_serializable_details(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                details={
                    "invalid": object(),
                },
            )

    # ========================================================
    # ПОЛУЧЕНИЕ IP-АДРЕСА
    # ========================================================

    def test_request_ip_returns_ipv4_address(self):

        request = self.factory.get(
            "/",
            REMOTE_ADDR="192.0.2.15",
        )

        self.assertEqual(
            get_request_ip(request),
            "192.0.2.15",
        )

    def test_request_ip_returns_ipv6_address(self):

        request = self.factory.get(
            "/",
            REMOTE_ADDR="2001:db8::1",
        )

        self.assertEqual(
            get_request_ip(request),
            "2001:db8::1",
        )

    def test_request_ip_returns_none_without_address(self):

        request = SimpleNamespace(
            META={},
        )

        self.assertIsNone(
            get_request_ip(request),
        )

    def test_request_ip_rejects_invalid_address(self):

        request = self.factory.get(
            "/",
            REMOTE_ADDR="999.999.999.999",
        )

        self.assertIsNone(
            get_request_ip(request),
        )

    def test_event_receives_ip_from_request(self):

        request = self.factory.get(
            "/",
            REMOTE_ADDR="192.0.2.20",
        )

        event = self.record_test_event(
            request=request,
        )

        self.assertEqual(
            event.ip_address,
            "192.0.2.20",
        )

    def test_explicit_ip_overrides_request_ip(self):

        request = self.factory.get(
            "/",
            REMOTE_ADDR="192.0.2.20",
        )

        event = self.record_test_event(
            request=request,
            ip_address="192.0.2.30",
        )

        self.assertEqual(
            event.ip_address,
            "192.0.2.30",
        )

    def test_invalid_explicit_ip_is_rejected(self):

        with self.assertRaises(AuditServiceError):
            self.record_test_event(
                ip_address="invalid-ip",
            )

    # ========================================================
    # ТРАНЗАКЦИОННОСТЬ
    # ========================================================

    def test_event_is_rolled_back_with_outer_transaction(self):

        initial_count = AuditEvent.objects.count()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():

                record_event(
                    actor=self.user,
                    action=AuditAction.UPDATE_STARTED,
                    obj=self.create_dummy_object(),
                )

                raise RuntimeError(
                    "Тестовый откат транзакции."
                )

        self.assertEqual(
            AuditEvent.objects.count(),
            initial_count,
        )

    def test_successful_transaction_saves_event(self):

        initial_count = AuditEvent.objects.count()

        with transaction.atomic():

            event = record_event(
                actor=self.user,
                action=AuditAction.ROLLBACK_STARTED,
                obj=self.create_dummy_object(),
            )

        self.assertEqual(
            AuditEvent.objects.count(),
            initial_count + 1,
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                pk=event.pk,
            ).exists()
        )
