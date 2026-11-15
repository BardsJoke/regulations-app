
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from deployments.adapters.registry import get_deployment_adapter
from deployments.models import DeploymentTarget


DEFAULT_CONFIG_KEYS = (
    "tests.update_target",
    "tests.rollback_target",
)


class Command(BaseCommand):
    help = (
        "Подготавливает лабораторные окружения, "
        "устанавливая начальную версию приложения из ZIP-пакета."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--package",
            type=str,
            default=None,
            help="Путь к ZIP-пакету начальной версии.",
        )

        parser.add_argument(
            "--package-version",
            dest="package_version",
            type=str,
            default="1.0.0",
            help="Ожидаемая версия пакета.",
        )

        parser.add_argument(
            "--config-key",
            action="append",
            dest="config_keys",
            help=(
                "Ключ целевого окружения. "
                "Аргумент можно передать несколько раз."
            ),
        )

    def handle(self, *args, **options):
        package_value = options["package"]

        if package_value:
            package_path = Path(package_value).expanduser()
        else:
            package_path = (
                Path(settings.BASE_DIR).resolve().parent
                / "regulations_App"
                / "tests_packages"
                / "museum_demo_v1.0.0.zip"
            )

        try:
            package_path = package_path.resolve(strict=True)
        except OSError as exc:
            raise CommandError(
                f"ZIP-пакет не найден: {package_path}"
            ) from exc

        if not package_path.is_file():
            raise CommandError(
                f"Указанный путь не является файлом: {package_path}"
            )

        expected_version = options["package_version"]

        if not expected_version:
            raise CommandError(
                "Необходимо указать версию пакета."
            )

        config_keys = options["config_keys"] or list(
            DEFAULT_CONFIG_KEYS
        )

        configured_targets = getattr(
            settings,
            "DEPLOYMENT_TARGETS",
            {},
        )

        if not isinstance(configured_targets, dict):
            raise CommandError(
                "DEPLOYMENT_TARGETS должен быть словарём."
            )

        prepared_targets = []

        # Сначала проверяем все окружения.
        # До завершения проверок установка не начинается.
        for config_key in config_keys:
            if config_key not in configured_targets:
                raise CommandError(
                    f"В DEPLOYMENT_TARGETS отсутствует ключ "
                    f"'{config_key}'."
                )

            target = (
                DeploymentTarget.objects
                .select_related("software")
                .filter(config_key=config_key)
                .first()
            )

            if target is None:
                raise CommandError(
                    f"В базе данных не найден DeploymentTarget "
                    f"с config_key='{config_key}'. "
                    f"Сначала создайте целевое окружение."
                )

            if not target.is_configured:
                raise CommandError(
                    f"Окружение '{config_key}' не настроено."
                )

            if not target.is_active:
                raise CommandError(
                    f"Окружение '{config_key}' неактивно."
                )

            if not target.software.is_active:
                raise CommandError(
                    f"Программное обеспечение для '{config_key}' "
                    f"неактивно."
                )

            try:
                adapter = get_deployment_adapter(target)
                inspection = adapter.inspect()
            except Exception as exc:
                raise CommandError(
                    f"Ошибка проверки окружения '{config_key}': {exc}"
                ) from exc

            if inspection.installed:
                raise CommandError(
                    f"В окружении '{config_key}' уже установлена "
                    f"версия {inspection.current_version}. "
                    f"Повторная первоначальная установка запрещена."
                )

            prepared_targets.append((target, adapter))

        self.stdout.write(
            f"ZIP-пакет: {package_path}"
        )
        self.stdout.write(
            f"Ожидаемая версия: {expected_version}"
        )
        self.stdout.write(
            f"Количество окружений: {len(prepared_targets)}"
        )

        # Все окружения прошли предварительные проверки.
        for target, adapter in prepared_targets:
            try:
                result = adapter.initialize_from_package(
                    package_path,
                    expected_version=expected_version,
                )

                target.last_known_version = result.version
                target.last_inspected_at = timezone.now()

                target.save(
                    update_fields=[
                        "last_known_version",
                        "last_inspected_at",
                        "updated_at",
                    ]
                )

            except Exception as exc:
                raise CommandError(
                    f"Не удалось подготовить окружение "
                    f"'{target.config_key}': {exc}"
                ) from exc

            self.stdout.write(
                self.style.SUCCESS(
                    f"Окружение '{target.config_key}' подготовлено. "
                    f"Версия: {result.version}. "
                    f"Release ID: {result.release_id}"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Подготовка лабораторных окружений завершена."
            )
        )
