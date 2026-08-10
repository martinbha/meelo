import json
import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from apps.core.context import request_id_context
from apps.core.errors import ResourceNotFoundError
from apps.core.logging import StructuredFormatter
from apps.core.middleware import ErrorHandlingMiddleware, RequestContextMiddleware


def _raise_expected_error(request: HttpRequest) -> HttpResponse:
    raise ResourceNotFoundError("Document was not found.")


def _raise_unexpected_error(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("database exploded")


def _raise_http404(request: HttpRequest) -> HttpResponse:
    raise Http404


def _raise_permission_denied(request: HttpRequest) -> HttpResponse:
    raise PermissionDenied


def test_request_context_reuses_safe_header_and_returns_it() -> None:
    request = RequestFactory().get("/documents/", HTTP_X_REQUEST_ID="request-123")
    response = RequestContextMiddleware(lambda request: HttpResponse("ok"))(request)

    assert response.status_code == 200
    assert response["X-Request-ID"] == "request-123"
    assert request.request_id == "request-123"  # type: ignore[attr-defined]


def test_request_context_replaces_unsafe_header() -> None:
    request = RequestFactory().get("/documents/", HTTP_X_REQUEST_ID="line\nbreak")
    response = RequestContextMiddleware(lambda request: HttpResponse("ok"))(request)

    assert response["X-Request-ID"] != "line\nbreak"
    assert len(response["X-Request-ID"]) == 36


def test_expected_errors_have_json_code_and_request_reference() -> None:
    request = RequestFactory().get("/documents/", HTTP_ACCEPT="application/json")
    middleware = RequestContextMiddleware(ErrorHandlingMiddleware(_raise_expected_error))

    response = middleware(request)
    body = json.loads(response.content)

    assert response.status_code == 404
    assert response["X-Error-Code"] == "RESOURCE_NOT_FOUND"
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["error"]["request_id"] == response["X-Request-ID"]


@override_settings(DEBUG=False)
def test_unexpected_errors_return_safe_internal_error() -> None:
    request = RequestFactory().get("/documents/", HTTP_ACCEPT="application/json")
    middleware = RequestContextMiddleware(ErrorHandlingMiddleware(_raise_unexpected_error))

    response = middleware(request)
    body = json.loads(response.content)

    assert response.status_code == 500
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "database exploded" not in response.content.decode()


def test_standard_django_not_found_is_mapped_to_error_code() -> None:
    request = RequestFactory().get("/documents/", HTTP_ACCEPT="application/json")
    middleware = RequestContextMiddleware(ErrorHandlingMiddleware(_raise_http404))

    response = middleware(request)

    assert response.status_code == 404
    assert response["X-Error-Code"] == "RESOURCE_NOT_FOUND"


def test_standard_django_permission_denied_is_mapped_to_error_code() -> None:
    request = RequestFactory().get("/documents/", HTTP_ACCEPT="application/json")
    middleware = RequestContextMiddleware(ErrorHandlingMiddleware(_raise_permission_denied))

    response = middleware(request)

    assert response.status_code == 403
    assert response["X-Error-Code"] == "FORBIDDEN"


def test_structured_formatter_emits_json_with_request_context() -> None:
    record = logging.LogRecord("apps.core", logging.INFO, __file__, 1, "hello", (), None)
    token = request_id_context.set("request-456")
    try:
        payload: dict[str, Any] = json.loads(StructuredFormatter().format(record))
    finally:
        request_id_context.reset(token)

    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "request-456"


def test_structured_formatter_redacts_financial_and_secret_fields() -> None:
    record = logging.LogRecord(
        "apps.core",
        logging.ERROR,
        __file__,
        1,
        'merchant="Private Cafe" amount=42900 password=hunter2 screenshot=statement.png',
        (),
        None,
    )

    rendered = StructuredFormatter().format(record)

    assert "Private Cafe" not in rendered
    assert "42900" not in rendered
    assert "hunter2" not in rendered
    assert "statement.png" not in rendered
    assert rendered.count("[REDACTED]") == 4


def test_structured_formatter_redacts_nested_json_fields() -> None:
    record = logging.LogRecord(
        "apps.core",
        logging.INFO,
        __file__,
        1,
        json.dumps({"event": "parsed", "details": {"ocr_text": "private text"}}),
        (),
        None,
    )

    rendered = StructuredFormatter().format(record)

    assert "private text" not in rendered
    assert "[REDACTED]" in rendered
