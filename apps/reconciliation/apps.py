import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class ReconciliationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reconciliation"

    def ready(self) -> None:
        logger.info(
            "Automatic reconciliation merge policy enabled=%s",
            settings.AUTOMATIC_RECONCILIATION_MERGE_ENABLED,
        )
