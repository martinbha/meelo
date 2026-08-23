from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .context import request_id_context
from .errors import ApplicationError, definition_for

logger = logging.getLogger("django.request")

ERROR_TEMPLATES = {
    400: "errors/400.html",
    403: "errors/403.html",
    404: "errors/404.html",
    500: "errors/500.html",
}
ERROR_CODES = {
    400: "INVALID_REQUEST",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    500: "INTERNAL_SERVER_ERROR",
}
ERROR_HEADINGS = {
    400: "Bad request",
    403: "Access denied",
    404: "Page not found",
    500: "Something went wrong",
}


def _request_id(request: HttpRequest) -> str:
    request_id = getattr(request, "request_id", "") or request_id_context.get()
    return request_id if request_id and request_id != "-" else str(uuid4())


def render_error_page(
    request: HttpRequest,
    *,
    status: int,
    error: ApplicationError | None = None,
) -> HttpResponse:
    """Render a safe full-page or HTMX-compatible response for one status."""

    response_status = status
    template_status = status if status in ERROR_TEMPLATES else 400
    code = error.code if error is not None else ERROR_CODES[template_status]
    definition = definition_for(code)
    request_id = _request_id(request)
    response = render(
        request,
        ERROR_TEMPLATES[template_status],
        {
            "error_code": code,
            "heading": ERROR_HEADINGS[template_status],
            "status_code": response_status,
            "message": error.public_message if error is not None else definition.message,
            "recovery_hint": (
                error.public_recovery_hint if error is not None else definition.recovery_hint
            ),
            "request_id": request_id,
        },
        status=response_status,
    )
    response["X-Request-ID"] = request_id
    response["X-Error-Code"] = code
    logger.warning("Rendered safe error page: %s", code)
    return response


def bad_request(request: HttpRequest, exception: Any) -> HttpResponse:
    del exception
    return render_error_page(request, status=400)


def permission_denied(request: HttpRequest, exception: Any) -> HttpResponse:
    del exception
    return render_error_page(request, status=403)


def page_not_found(request: HttpRequest, exception: Any) -> HttpResponse:
    del exception
    return render_error_page(request, status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    return render_error_page(request, status=500)
