from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .contracts import OcrConfigurationError
from .tesseract import inspect_tesseract_installation


class OcrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ocr"

    def ready(self) -> None:
        if not settings.OCR_VERIFY_TESSERACT_INSTALLATION:
            return
        try:
            inspect_tesseract_installation()
        except OcrConfigurationError as exc:
            raise ImproperlyConfigured(f"Tesseract startup verification failed: {exc}") from exc
