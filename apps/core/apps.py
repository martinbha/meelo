from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        from .sentry import configure_sentry

        configure_sentry()
        if settings.FIELD_ENCRYPTION_MASTER_KEY_REQUIRED:
            from .key_management import load_master_key

            load_master_key()
