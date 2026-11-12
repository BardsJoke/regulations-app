
import ipaddress
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import AuditEvent


# ============================================================
# ТИПЫ СОБЫТИЙ АУДИТА
# ============================================================

class AuditAction:
    """
    Единый справочник кодов событий аудита.

    Используется всеми приложениями Regulations.
    """

    # Карточки изменений
    CHANGE_CREATED = "change.created"
    CHANGE_UPDATED = "change.updated"
    CHANGE_SUBMITTED = "change.submitted"
    CHANGE_APPROVED = "change.approved"
    CHANGE_REJECTED = "change.rejected"

    # Операции
    UPDATE_STARTED = "operation.update.started"
    ROLLBACK_STARTED = "operation.rollback.started"
    OPERATION_COMPLETED = "operation.completed"
    OPERATION_FAILED = "operation.failed"

    # Выполнение шагов
    STEP_STARTED = "operation.step.started"
    STEP_COMPLETED = "operation.step.completed"
    STEP_FAILED = "operation.step.failed"

    # Резервные копии
    BACKUP_CREATED = "backup.created"
    BACKUP_VERIFIED = "backup.verified"
    BACKUP_VERIFICATION_FAILED = "backup.verification_failed"

    # Управление доступом
    ROLE_ASSIGNED = "access.role_assigned"
    ROLE_REVOKED = "access.role_revoked"


class AuditServiceError(ValidationError):
    """Ошибка при регистрации события аудита."""


# ============================================================
# ПОЛУЧЕНИЕ IP-АДРЕСА
# ============================================================

def get_request_ip(request):
    """
    Получает IP-адрес из HTTP-запроса.

    Используется REMOTE_ADDR.

    X-Forwarded-For намеренно не используется:
    этот заголовок нельзя считать достоверным,
    пока не настроены доверенные прокси.
    """

    if request is None:
        return None

    meta = getattr(request, "META", {})

    remote_addr = meta.get("REMOTE_ADDR")

    if not remote_addr:
        return None

    try:
        address = ipaddress.ip_address(remote_addr.strip())
    except (ValueError, AttributeError, TypeError):
        return None

    return str(address)


# ============================================================
# ВАЛИДАЦИЯ ПОЛЬЗОВАТЕЛЯ
# ============================================================

def _validate_actor(actor):
    """
    Проверяет, что событие можно связать
    с существующей пользовательской записью.
    """

    if actor is None or not getattr(actor, "pk", None):
        raise AuditServiceError(
            "Для регистрации события необходимо указать пользователя."
        )


# ============================================================
# ВАЛИДАЦИЯ ОБЪЕКТА
# ============================================================

def _resolve_object(obj, object_type, object_id):
    """
    Определяет тип и UUID объекта аудита.

    Можно передать объект Django напрямую
    или указать object_type и object_id отдельно.
    """

    if obj is not None:

        if not getattr(obj, "pk", None):
            raise AuditServiceError(
                "Нельзя зарегистрировать событие для "
                "несохранённого объекта."
            )

        object_type = (
            object_type
            or (
                f"{obj._meta.app_label}."
                f"{obj._meta.model_name}"
            )
        )

        if object_id is None:
            object_id = obj.pk

    if not object_type or not isinstance(object_type, str):
        raise AuditServiceError(
            "Необходимо указать корректный тип объекта."
        )

    object_type = object_type.strip()

    if not object_type:
        raise AuditServiceError(
            "Тип объекта не может быть пустым."
        )

    if object_id is None:
        raise AuditServiceError(
            "Необходимо указать идентификатор объекта."
        )

    try:
        object_id = uuid.UUID(str(object_id))
    except (ValueError, TypeError, AttributeError):
        raise AuditServiceError(
            "Идентификатор объекта должен иметь формат UUID."
        )

    return object_type, object_id


# ============================================================
# ЗАПИСЬ СОБЫТИЯ
# ============================================================

@transaction.atomic
def record_event(
    *,
    actor,
    action,
    obj=None,
    object_type=None,
    object_id=None,
    details=None,
    request=None,
    ip_address=None,
):
    """
    Универсальная регистрация события аудита.

    Параметры:

    actor:
        Пользователь, совершивший действие.

    action:
        Код события из AuditAction либо собственная строка.

    obj:
        Django-объект, к которому относится событие.

    object_type и object_id:
        Альтернатива obj.
        Используется, когда исходного объекта уже нет.

    details:
        Дополнительные сведения в формате JSON-словаря.

    request:
        HTTP-запрос. IP определяется через REMOTE_ADDR.

    ip_address:
        Явно переданный IP-адрес.
        Имеет приоритет над request.

    Возвращает созданный AuditEvent.
    """

    # --------------------------------------------------------
    # 1. ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ
    # --------------------------------------------------------

    _validate_actor(actor)

    # --------------------------------------------------------
    # 2. ПРОВЕРКА ДЕЙСТВИЯ
    # --------------------------------------------------------

    if not isinstance(action, str) or not action.strip():
        raise AuditServiceError(
            "Необходимо указать код события аудита."
        )

    action = action.strip()

    # --------------------------------------------------------
    # 3. ОПРЕДЕЛЕНИЕ ОБЪЕКТА
    # --------------------------------------------------------

    object_type, object_id = _resolve_object(
        obj=obj,
        object_type=object_type,
        object_id=object_id,
    )

    # --------------------------------------------------------
    # 4. ПРОВЕРКА ДОПОЛНИТЕЛЬНЫХ ДАННЫХ
    # --------------------------------------------------------

    if details is None:
        details = {}

    if not isinstance(details, dict):
        raise AuditServiceError(
            "Подробности события должны быть словарём."
        )

    # --------------------------------------------------------
    # 5. ОПРЕДЕЛЕНИЕ IP-АДРЕСА
    # --------------------------------------------------------

    if ip_address is None:
        ip_address = get_request_ip(request)

    elif ip_address:
        try:
            ip_address = str(
                ipaddress.ip_address(ip_address)
            )
        except (ValueError, TypeError):
            raise AuditServiceError(
                "Указан некорректный IP-адрес."
            )

    else:
        ip_address = None

    # --------------------------------------------------------
    # 6. СОЗДАНИЕ СОБЫТИЯ
    # --------------------------------------------------------

    try:
        event = AuditEvent.record(
            user=actor,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details=details,
            ip_address=ip_address,
        )

    except ValidationError as error:
        raise AuditServiceError(
            error.message_dict
            if hasattr(error, "message_dict")
            else error.messages
        ) from error

    return event
