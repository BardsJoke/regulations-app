from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccessConfig(AppConfig):
    name = "access"
    verbose_name = "Доступы"

    def ready(self):
        from . import signals

        post_migrate.connect(
            signals.sync_wagtail_access_group,
            sender=self,
            dispatch_uid="access.sync_wagtail_access_group",
        )