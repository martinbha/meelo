from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory

from apps.core.error_views import (
    bad_request,
    page_not_found,
    permission_denied,
    server_error,
)
from apps.core.logging import RequestContextFilter, StructuredFormatter
from apps.core.middleware import RequestContextMiddleware

ErrorHandler = Callable[..., HttpResponse]


class CapturingHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.parametrize(
    ("handler", "status", "code"),
    [
        (bad_request, 400, "INVALID_REQUEST"),
        (permission_denied, 403, "FORBIDDEN"),
        (page_not_found, 404, "RESOURCE_NOT_FOUND"),
    ],
)
def test_status_handlers_render_safe_full_pages(
    handler: ErrorHandler, status: int, code: str
) -> None:
    request = RequestFactory().get("/missing/", HTTP_X_REQUEST_ID=f"request-{status}")
    request.user = type("AnonymousUser", (), {"is_authenticated": False})()

    def get_response(request: HttpRequest) -> HttpResponse:
        return handler(request, None)

    response = RequestContextMiddleware(get_response)(request)
    content = response.content.decode()

    assert response.status_code == status
    assert response["X-Error-Code"] == code
    assert response["X-Request-ID"] == f"request-{status}"
    assert "<!doctype html>" in content
    assert "Traceback" not in content
    assert "DJANGO_SECRET_KEY" not in content
    assert "SELECT *" not in content


def test_server_error_handler_renders_without_exception_details() -> None:
    request = RequestFactory().get("/broken/", HTTP_X_REQUEST_ID="request-500")
    request.user = type("AnonymousUser", (), {"is_authenticated": False})()

    response = RequestContextMiddleware(lambda request: server_error(request))(request)
    content = response.content.decode()

    assert response.status_code == 500
    assert response["X-Error-Code"] == "INTERNAL_SERVER_ERROR"
    assert response["X-Request-ID"] == "request-500"
    assert "Traceback" not in content
    assert "settings" not in content.lower()


def test_generated_error_page_request_id_is_used_by_structured_log() -> None:
    request = RequestFactory().get("/broken/")
    request.user = type("AnonymousUser", (), {"is_authenticated": False})()
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("django.request")
    handler = CapturingHandler(records)
    handler.addFilter(RequestContextFilter())
    logger.addHandler(handler)
    try:
        response = page_not_found(request, None)
    finally:
        logger.removeHandler(handler)

    assert records
    logged = json.loads(StructuredFormatter().format(records[-1]))
    assert logged["request_id"] == response["X-Request-ID"]


def test_missing_route_renders_safe_full_page_with_request_reference(client: Client) -> None:
    response = client.get("/does-not-exist/", HTTP_X_REQUEST_ID="missing-page")
    content = response.content.decode()

    assert response.status_code == 404
    assert response["X-Request-ID"] == "missing-page"
    assert "Page not found" in content
    assert "missing-page" in content
    assert "Traceback" not in content


def test_missing_route_renders_content_only_for_htmx(client: Client) -> None:
    response = client.get(
        "/does-not-exist/",
        HTTP_HX_REQUEST="true",
        HTTP_X_REQUEST_ID="htmx-missing",
    )
    content = response.content.decode()

    assert response.status_code == 404
    assert response["X-Request-ID"] == "htmx-missing"
    assert "error-page" in content
    assert "htmx-missing" in content
    assert "<!doctype html>" not in content
    assert "<header" not in content
