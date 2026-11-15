
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from access.models import ModuleRole, RoleAssignment
from audit.services import AuditAction, record_event
from catalogs.models import (
    CardStatus,
    CardStatusCode,
    SoftwarePackage,
    SoftwarePackageStatus,
)
from deployments.models import DeploymentTarget

from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeTicket,
    HashStatus,
    PlanStep,
    StepKind,
)


class ChangeWorkflowError(ValidationError):
    """Ошибка нарушения правил бизнес-процесса."""


_UNSET = object()


def _require_role(user, role):
    """Проверяет активность пользователя и его роль в модуле."""

    if (
        not user
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        raise PermissionDenied("Необходимо войти в систему.")

    has_role = RoleAssignment.objects.filter(
        user=user,
        role=role,
    ).exists()

    if not has_role:
        raise PermissionDenied(
            "У вас нет необходимой роли для этого действия."
        )


def _require_ticket_creator(actor, ticket):
    """Разрешает исполнителю работать только со своей карточкой."""

    if ticket.created_by_id != actor.pk:
        raise PermissionDenied(
            "Вы можете изменять только созданные вами карточки."
        )


def _require_not_creator(actor, ticket):
    """Запрещает руководителю согласовывать собственную карточку."""

    if ticket.created_by_id == actor.pk:
        raise PermissionDenied(
            "Нельзя принимать решение по собственной карточке."
        )


def _get_status(code):
    """Получает системный статус из справочника."""

    try:
        return CardStatus.objects.get(code=code)
    except CardStatus.DoesNotExist:
        raise ChangeWorkflowError(
            f"Системный статус '{code}' отсутствует в справочнике."
        )


def _get_locked_ticket(ticket_id):
    """Получает карточку с блокировкой строки на время транзакции."""

    try:
        return (
            ChangeTicket.objects
            # deployment_target и software_package — nullable FK.
            # На PostgreSQL нельзя блокировать nullable-side LEFT JOIN;
            # блокируем только строку карточки. Целевая установка и пакет
            # блокируются отдельно в соответствующих helper-функциях.
            .select_for_update(of=("self",))
            .select_related(
                "status",
                "software",
                "deployment_target",
                "deployment_target__software",
                "software_package",
                "software_package__software",
            )
            .get(pk=ticket_id)
        )
    except ChangeTicket.DoesNotExist:
        raise ChangeWorkflowError("Карточка изменения не найдена.")


def _get_locked_deployment_target(target_or_id):
    """
    Получает целевую установку с блокировкой строки.

    Принимает как объект DeploymentTarget, так и его UUID.
    Повторно загружает объект из базы, чтобы работать
    с актуальными данными внутри транзакции.
    """

    target_id = getattr(target_or_id, "pk", target_or_id)

    if not target_id:
        raise ChangeWorkflowError(
            "Необходимо выбрать целевую установку."
        )

    try:
        return (
            DeploymentTarget.objects
            .select_for_update()
            .select_related("software")
            .get(pk=target_id)
        )
    except DeploymentTarget.DoesNotExist:
        raise ChangeWorkflowError(
            "Выбранная целевая установка не найдена."
        )


def _get_locked_software_package(package_or_id, *, software_id):
    """Получает и проверяет доступный пакет из библиотеки релизов."""

    package_id = getattr(package_or_id, "pk", package_or_id)

    if not package_id:
        raise ChangeWorkflowError(
            "Необходимо выбрать пакет обновления."
        )

    try:
        package = (
            SoftwarePackage.objects
            .select_for_update()
            .select_related("software")
            .get(pk=package_id)
        )
    except SoftwarePackage.DoesNotExist:
        raise ChangeWorkflowError(
            "Выбранный пакет не найден в библиотеке."
        )

    if package.software_id != software_id:
        raise ChangeWorkflowError(
            "Выбранный пакет относится к другому программному обеспечению."
        )

    if (
        package.status != SoftwarePackageStatus.VALID
        or not package.is_usable
    ):
        raise ChangeWorkflowError(
            "Пакет не прошёл проверку или недоступен."
        )

    try:
        file_exists = bool(
            package.package_file
            and package.package_file.storage.exists(
                package.package_file.name
            )
        )
    except Exception as exc:
        raise ChangeWorkflowError(
            "Не удалось проверить наличие ZIP-файла пакета."
        ) from exc

    if not file_exists:
        raise ChangeWorkflowError(
            "Файл выбранного пакета отсутствует в хранилище."
        )

    return package


def _validate_ticket_software_package(ticket):
    """Повторно проверяет пакет карточки перед согласованием."""

    if not ticket.software_package_id:
        # Исторические карточки могут использовать прежний package_file.
        return None

    package = _get_locked_software_package(
        ticket.software_package_id,
        software_id=ticket.software_id,
    )

    if ticket.target_version != package.version:
        raise ChangeWorkflowError(
            "Целевая версия карточки не совпадает с версией пакета."
        )

    if (ticket.expected_sha256 or "").lower() != package.sha256.lower():
        raise ChangeWorkflowError(
            "Ожидаемая SHA-256 карточки не совпадает с пакетом."
        )

    if ticket.package_file.name != package.package_file.name:
        raise ChangeWorkflowError(
            "Файл карточки не совпадает с файлом выбранного пакета."
        )

    return package


def _require_usable_deployment_target(target):
    """
    Проверяет, разрешено ли использовать установку
    для создания или согласования изменения.
    """

    if not target.is_active:
        raise ChangeWorkflowError(
            "Выбранная целевая установка неактивна."
        )

    if not target.is_configured:
        raise ChangeWorkflowError(
            "Целевая установка ещё не настроена."
        )

    if not target.software.is_active:
        raise ChangeWorkflowError(
            "Программное обеспечение этой установки неактивно."
        )


def _validate_ticket_target(ticket):
    """
    Проверяет наличие и доступность установки у карточки.

    Возвращает актуальную целевую установку,
    заблокированную на время текущей транзакции.
    """

    if not ticket.deployment_target_id:
        raise ChangeWorkflowError(
            "Для этой карточки не выбрана целевая установка. "
            "Укажите установку перед продолжением согласования."
        )

    target = _get_locked_deployment_target(
        ticket.deployment_target_id
    )

    if target.software_id != ticket.software_id:
        raise ChangeWorkflowError(
            "Целевая установка относится к другому "
            "программному обеспечению."
        )

    _require_usable_deployment_target(target)
    _validate_ticket_software_package(ticket)

    return target


def _validate_new_window(start_at, end_at):
    """Проверяет новые даты планового окна."""

    if start_at is None or end_at is None:
        raise ChangeWorkflowError(
            "Необходимо указать начало и окончание планового окна."
        )

    if start_at >= end_at:
        raise ChangeWorkflowError(
            "Окончание планового окна должно быть позже начала."
        )


# ---------------------------------------------------------
# СОЗДАНИЕ КАРТОЧКИ
# ---------------------------------------------------------

@transaction.atomic
def create_draft(
    *,
    actor,
    deployment_target,
    source_version="",
    target_version="",
    risks="",
    window_start_at=None,
    window_end_at=None,
    software_package=None,
):
    """
    Создаёт новую карточку в статусе draft.

    Программное обеспечение определяется через выбранную
    целевую установку. Если передан software_package,
    версия, SHA-256 и путь ZIP берутся из библиотеки релизов.

    Параметр software_package пока необязателен для сохранения
    совместимости со старыми вызывающими функциями. Новый UI
    должен требовать выбор пакета для новых заявок.
    """

    _require_role(actor, ModuleRole.EXECUTOR)

    target = _get_locked_deployment_target(deployment_target)
    _require_usable_deployment_target(target)

    software = target.software
    package = None

    if software_package is not None:
        package = _get_locked_software_package(
            software_package,
            software_id=software.pk,
        )
        target_version = package.version

    draft_status = _get_status(CardStatusCode.DRAFT)

    ticket = ChangeTicket(
        software=software,
        deployment_target=target,
        software_package=package,
        created_by=actor,
        status=draft_status,
        source_version=source_version,
        target_version=target_version,
        risks=risks,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )

    if package is not None:
        # Ссылаемся на тот же объект в storage, не копируя ZIP.
        ticket.package_file = package.package_file.name
        ticket.expected_sha256 = package.sha256.lower()

    ticket.full_clean()
    ticket.save()

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_CREATED,
        obj=ticket,
        details={
            "software_id": str(ticket.software_id),
            "deployment_target_id": str(target.pk),
            "software_package_id": (
                str(package.pk) if package else None
            ),
            "package_version": package.version if package else None,
            "environment": target.environment,
            "adapter_type": target.adapter_type,
            "status": ticket.status.code,
            "source_version": ticket.source_version,
            "target_version": ticket.target_version,
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТПРАВКА НА СОГЛАСОВАНИЕ
# ---------------------------------------------------------

@transaction.atomic
def submit_for_approval(*, ticket_id, actor):
    """Отправляет собственный черновик на согласование."""

    _require_role(actor, ModuleRole.EXECUTOR)

    ticket = _get_locked_ticket(ticket_id)

    _require_ticket_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.DRAFT:
        raise ChangeWorkflowError(
            "На согласование можно отправить только черновик."
        )

    # Проверяем, что карточка связана с доступной установкой.
    _validate_ticket_target(ticket)

    _validate_new_window(
        ticket.window_start_at,
        ticket.window_end_at,
    )

    ticket.clean()

    has_backup_step = PlanStep.objects.filter(
        ticket=ticket,
        step_order=1,
        step_kind=StepKind.BACKUP,
    ).exists()

    if not has_backup_step:
        raise ChangeWorkflowError(
            "Первым шагом должно быть резервное копирование."
        )

    ticket.status = _get_status(CardStatusCode.PENDING)
    ticket.rejection_comment = ""

    ticket.save(
        update_fields=[
            "status",
            "rejection_comment",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_SUBMITTED,
        obj=ticket,
        details={
            "status_before": CardStatusCode.DRAFT,
            "status_after": ticket.status.code,
            "deployment_target_id": str(ticket.deployment_target_id),
        },
    )

    return ticket


# ---------------------------------------------------------
# СОГЛАСОВАНИЕ
# ---------------------------------------------------------

@transaction.atomic
def approve_ticket(*, ticket_id, actor, comment=""):
    """Согласовывает карточку руководителем."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.PENDING:
        raise ChangeWorkflowError(
            "Согласовать можно только карточку на согласовании."
        )

    # Повторно проверяем установку непосредственно перед решением.
    _validate_ticket_target(ticket)

    old_status = ticket.status

    ticket.status = _get_status(CardStatusCode.APPROVED)
    ticket.approved_by = actor
    ticket.rejection_comment = ""

    ticket.save(
        update_fields=[
            "status",
            "approved_by",
            "rejection_comment",
            "updated_at",
        ]
    )

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.APPROVED,
        comment=comment.strip(),
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_APPROVED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": old_status.code,
            "status_after": ticket.status.code,
            "deployment_target_id": str(ticket.deployment_target_id),
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТКЛОНЕНИЕ
# ---------------------------------------------------------

@transaction.atomic
def reject_ticket(*, ticket_id, actor, comment):
    """Отклоняет карточку и возвращает её в черновик."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.PENDING:
        raise ChangeWorkflowError(
            "Отклонить можно только карточку на согласовании."
        )

    comment = comment.strip()

    if not comment:
        raise ChangeWorkflowError(
            "При отклонении необходимо указать комментарий."
        )

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.REJECTED,
        comment=comment,
    )

    ticket.status = _get_status(CardStatusCode.DRAFT)
    ticket.rejection_comment = comment
    ticket.approved_by = None

    ticket.save(
        update_fields=[
            "status",
            "rejection_comment",
            "approved_by",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_REJECTED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": CardStatusCode.PENDING,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# ПЕРЕНОС ПЛАНОВОГО ОКНА
# ---------------------------------------------------------

@transaction.atomic
def reschedule_ticket(
    *,
    ticket_id,
    actor,
    new_start_at,
    new_end_at,
    comment="",
):
    """
    Переносит плановое окно.

    pending -> approved
    approved -> approved

    Перенос обязательно фиксируется в ApprovalRecord.
    """

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    current_status = ticket.status.code

    if current_status not in {
        CardStatusCode.PENDING,
        CardStatusCode.APPROVED,
    }:
        raise ChangeWorkflowError(
            "Перенести окно можно только у карточки "
            "на согласовании или уже согласованной карточки."
        )

    _validate_new_window(new_start_at, new_end_at)

    # Перенос карточки на согласовании фактически приводит
    # к её утверждению, поэтому установка должна быть доступна.
    if current_status == CardStatusCode.PENDING:
        _validate_ticket_target(ticket)

    old_start_at = ticket.window_start_at
    old_end_at = ticket.window_end_at

    ticket.window_start_at = new_start_at
    ticket.window_end_at = new_end_at

    ticket.clean()

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.RESCHEDULED,
        comment=comment.strip(),
        old_start_at=old_start_at,
        old_end_at=old_end_at,
        new_start_at=new_start_at,
        new_end_at=new_end_at,
    )

    update_fields = [
        "window_start_at",
        "window_end_at",
        "updated_at",
    ]

    if current_status == CardStatusCode.PENDING:
        ticket.status = _get_status(CardStatusCode.APPROVED)
        ticket.approved_by = actor

        update_fields.extend([
            "status",
            "approved_by",
        ])

    ticket.save(update_fields=update_fields)

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_RESCHEDULED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": current_status,
            "status_after": ticket.status.code,
            "old_window_start": (
                old_start_at.isoformat()
                if old_start_at else None
            ),
            "old_window_end": (
                old_end_at.isoformat()
                if old_end_at else None
            ),
            "new_window_start": new_start_at.isoformat(),
            "new_window_end": new_end_at.isoformat(),
        },
    )

    return ticket


# ---------------------------------------------------------
# ОТМЕНА
# ---------------------------------------------------------

@transaction.atomic
def cancel_ticket(*, ticket_id, actor, comment=""):
    """Отменяет карточку на согласовании или согласованную."""

    _require_role(actor, ModuleRole.APPROVER)

    ticket = _get_locked_ticket(ticket_id)

    _require_not_creator(actor, ticket)

    if ticket.status.code not in {
        CardStatusCode.PENDING,
        CardStatusCode.APPROVED,
    }:
        raise ChangeWorkflowError(
            "Отменить можно только карточку на согласовании "
            "или согласованную карточку."
        )

    old_status = ticket.status.code

    approval_record = ApprovalRecord.objects.create(
        ticket=ticket,
        approver=actor,
        decision=ApprovalDecision.CANCELLED,
        comment=comment.strip(),
        old_start_at=ticket.window_start_at,
        old_end_at=ticket.window_end_at,
    )

    ticket.status = _get_status(CardStatusCode.CANCELLED)

    ticket.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_CANCELLED,
        obj=ticket,
        details={
            "approval_record_id": str(approval_record.pk),
            "status_before": old_status,
            "status_after": ticket.status.code,
        },
    )

    return ticket


# ---------------------------------------------------------
# РЕДАКТИРОВАНИЕ ЧЕРНОВИКА
# ---------------------------------------------------------

@transaction.atomic
def update_draft(
    *,
    ticket_id,
    actor,
    deployment_target,
    source_version="",
    target_version="",
    risks="",
    window_start_at=None,
    window_end_at=None,
    software_package=_UNSET,
):
    """
    Обновляет существующий черновик.

    Исполнитель может редактировать только собственные
    карточки, пока они находятся в статусе draft.

    Если software_package не передан, существующая связь
    сохраняется, когда пакет подходит новому software.
    Явная передача None снимает связь с пакетом.
    """

    _require_role(actor, ModuleRole.EXECUTOR)

    ticket = _get_locked_ticket(ticket_id)

    _require_ticket_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.DRAFT:
        raise ChangeWorkflowError(
            "Редактировать можно только карточку в статусе «Черновик»."
        )

    target = _get_locked_deployment_target(deployment_target)
    _require_usable_deployment_target(target)
    software = target.software

    selected_package = None
    clear_existing_package = False

    if software_package is _UNSET:
        if ticket.software_package_id:
            if ticket.software_package.software_id == software.pk:
                selected_package = _get_locked_software_package(
                    ticket.software_package_id,
                    software_id=software.pk,
                )
            else:
                clear_existing_package = True
    elif software_package is None:
        clear_existing_package = bool(ticket.software_package_id)
    else:
        selected_package = _get_locked_software_package(
            software_package,
            software_id=software.pk,
        )

    # Для выбранного/сохраняемого пакета версия берётся из библиотеки.
    if selected_package is not None:
        target_version = selected_package.version

    changed_fields = []

    if ticket.software_id != software.pk:
        changed_fields.append("software")

    if ticket.deployment_target_id != target.pk:
        changed_fields.append("deployment_target")

    if ticket.source_version != source_version:
        changed_fields.append("source_version")

    if ticket.target_version != target_version:
        changed_fields.append("target_version")

    if ticket.risks != risks:
        changed_fields.append("risks")

    if ticket.window_start_at != window_start_at:
        changed_fields.append("window_start_at")

    if ticket.window_end_at != window_end_at:
        changed_fields.append("window_end_at")

    old_package = ticket.software_package if ticket.software_package_id else None

    ticket.software = software
    ticket.deployment_target = target
    ticket.source_version = source_version
    ticket.target_version = target_version
    ticket.risks = risks
    ticket.window_start_at = window_start_at
    ticket.window_end_at = window_end_at

    if selected_package is not None:
        package_selection_changed = (
            ticket.software_package_id != selected_package.pk
        )
        package_file_changed = (
            ticket.package_file.name != selected_package.package_file.name
        )
        package_checksum_changed = (
            (ticket.expected_sha256 or "").lower()
            != selected_package.sha256.lower()
        )

        if package_selection_changed:
            changed_fields.append("software_package")
        if package_file_changed:
            changed_fields.append("package_file")
        if package_checksum_changed:
            changed_fields.append("expected_sha256")

        ticket.software_package = selected_package
        ticket.target_version = selected_package.version
        ticket.package_file = selected_package.package_file.name
        ticket.expected_sha256 = selected_package.sha256.lower()

        # Сбрасываем результат проверки только при фактической смене
        # выбранного пакета, файла или контрольной суммы.
        if (
            package_selection_changed
            or package_file_changed
            or package_checksum_changed
        ):
            ticket.actual_sha256 = ""
            ticket.hash_status = HashStatus.NOT_CHECKED
            ticket.hash_checked_at = None

            changed_fields.extend([
                "actual_sha256",
                "hash_status",
                "hash_checked_at",
            ])
    elif clear_existing_package:
        if ticket.software_package_id:
            changed_fields.append("software_package")

        # Если старый файл был именно файлом библиотеки пакетов,
        # очищаем ссылку на него при отвязке/смене продукта.
        if (
            old_package is not None
            and ticket.package_file.name == old_package.package_file.name
        ):
            ticket.package_file = ""
            ticket.expected_sha256 = ""
            ticket.actual_sha256 = ""
            ticket.hash_status = HashStatus.NOT_CHECKED
            ticket.hash_checked_at = None

            changed_fields.extend([
                "package_file",
                "expected_sha256",
                "actual_sha256",
                "hash_status",
                "hash_checked_at",
            ])

        ticket.software_package = None

    ticket.full_clean()

    update_fields = [
        "software",
        "deployment_target",
        "software_package",
        "source_version",
        "target_version",
        "package_file",
        "expected_sha256",
        "actual_sha256",
        "hash_status",
        "hash_checked_at",
        "risks",
        "window_start_at",
        "window_end_at",
        "updated_at",
    ]

    ticket.save(update_fields=update_fields)

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_UPDATED,
        obj=ticket,
        details={
            "changed_fields": sorted(set(changed_fields)),
            "deployment_target_id": str(target.pk),
            "software_package_id": (
                str(ticket.software_package_id)
                if ticket.software_package_id
                else None
            ),
            "target_version": ticket.target_version,
            "status": ticket.status.code,
        },
    )
    return ticket


# ---------------------------------------------------------
# УПРАВЛЕНИЕ ПЛАНОВЫМИ ШАГАМИ
# ---------------------------------------------------------


def _get_locked_draft_for_plan(*, ticket_id, actor):
    """
    Получает карточку с блокировкой и проверяет,
    что исполнитель может изменять её план.
    """

    _require_role(actor, ModuleRole.EXECUTOR)

    ticket = _get_locked_ticket(ticket_id)

    _require_ticket_creator(actor, ticket)

    if ticket.status.code != CardStatusCode.DRAFT:
        raise ChangeWorkflowError(
            "Изменять плановые шаги можно только в черновике."
        )

    return ticket


@transaction.atomic
def add_plan_step(
    *,
    ticket_id,
    actor,
    step_kind,
    name,
    planned_duration_min,
    description="",
):
    """
    Добавляет новый шаг в конец плана черновика.

    Нумерация определяется автоматически.
    """

    ticket = _get_locked_draft_for_plan(
        ticket_id=ticket_id,
        actor=actor,
    )

    last_step_order = (
        PlanStep.objects
        .filter(ticket=ticket)
        .order_by("-step_order")
        .values_list("step_order", flat=True)
        .first()
        or 0
    )

    step = PlanStep(
        ticket=ticket,
        step_order=last_step_order + 1,
        step_kind=step_kind,
        name=name,
        description=description,
        planned_duration_min=planned_duration_min,
    )

    step.full_clean()
    step.save()

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_UPDATED,
        obj=ticket,
        details={
            "operation": "plan_step_added",
            "plan_step_id": str(step.pk),
            "step_order": step.step_order,
            "step_kind": step.step_kind,
            "name": step.name,
        },
    )

    return step


@transaction.atomic
def update_plan_step(
    *,
    ticket_id,
    step_id,
    actor,
    step_kind,
    name,
    planned_duration_min,
    description="",
):
    """
    Обновляет существующий плановый шаг.

    Порядковый номер не меняется.
    Для изменения порядка в MVP используется
    удаление последних шагов и их повторное добавление.
    """

    ticket = _get_locked_draft_for_plan(
        ticket_id=ticket_id,
        actor=actor,
    )

    try:
        step = (
            PlanStep.objects
            .select_for_update()
            .get(
                pk=step_id,
                ticket=ticket,
            )
        )
    except PlanStep.DoesNotExist:
        raise ChangeWorkflowError(
            "Плановый шаг не найден в этой карточке."
        )

    changed_fields = []

    new_values = {
        "step_kind": step_kind,
        "name": name,
        "description": description,
        "planned_duration_min": planned_duration_min,
    }

    for field, value in new_values.items():
        if getattr(step, field) != value:
            setattr(step, field, value)
            changed_fields.append(field)

    if not changed_fields:
        return step

    step.full_clean()

    step.save(
        update_fields=[
            *changed_fields,
            "updated_at",
        ]
    )

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_UPDATED,
        obj=ticket,
        details={
            "operation": "plan_step_updated",
            "plan_step_id": str(step.pk),
            "step_order": step.step_order,
            "changed_fields": changed_fields,
        },
    )

    return step


@transaction.atomic
def delete_plan_step(
    *,
    ticket_id,
    step_id,
    actor,
):
    """
    Удаляет последний шаг плана черновика.

    Удаление шагов из середины списка запрещено,
    чтобы не нарушить последовательность нумерации.
    """

    ticket = _get_locked_draft_for_plan(
        ticket_id=ticket_id,
        actor=actor,
    )

    try:
        step = (
            PlanStep.objects
            .select_for_update()
            .get(
                pk=step_id,
                ticket=ticket,
            )
        )
    except PlanStep.DoesNotExist:
        raise ChangeWorkflowError(
            "Плановый шаг не найден в этой карточке."
        )

    last_step_order = (
        PlanStep.objects
        .filter(ticket=ticket)
        .order_by("-step_order")
        .values_list("step_order", flat=True)
        .first()
    )

    if step.step_order != last_step_order:
        raise ChangeWorkflowError(
            "Сначала необходимо удалить последующие шаги плана."
        )

    step_data = {
        "plan_step_id": str(step.pk),
        "step_order": step.step_order,
        "step_kind": step.step_kind,
        "name": step.name,
    }

    step.delete()

    record_event(
        actor=actor,
        action=AuditAction.CHANGE_UPDATED,
        obj=ticket,
        details={
            "operation": "plan_step_deleted",
            **step_data,
        },
    )

    return ticket
