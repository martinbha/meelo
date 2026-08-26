from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from apps.ocr.contracts import OcrConfigurationError
from apps.ocr.tesseract import inspect_tesseract_installation

from .operational_health import worker_queue_summary


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Render the authenticated application landing page."""

    return render(request, "dashboard.html")


def health_check(request: HttpRequest) -> JsonResponse:
    """Report application and database readiness without exposing configuration."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    payload: dict[str, object] = {"status": "ok", **worker_queue_summary()}
    if settings.OCR_VERIFY_TESSERACT_INSTALLATION:
        try:
            installation = inspect_tesseract_installation()
        except OcrConfigurationError as exc:
            payload.update(status="error", tesseract={"status": "error", "message": str(exc)})
            return JsonResponse(payload, status=503)
        payload["tesseract"] = {
            "status": "ok",
            "binary_version": installation.binary_version,
            "languages": sorted(
                name.removeprefix("language_") for name in installation.language_versions
            ),
        }
    return JsonResponse(payload)
