from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse

from .context import request_id_context
from .error_views import render_error_page
from .errors import ApplicationError, ForbiddenError, InternalServerError, ResourceNotFoundError
from .key_scope import clear_scope

logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id(request: HttpRequest) -> str:
    candidate = request.headers.get("X-Request-ID", "")
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def _error_response(request: HttpRequest, error: ApplicationError) -> HttpResponse:
    request_id = request_id_context.get()
    payload = {
        "error": {
            "code": error.code,
            "message": error.public_message,
            "recovery_hint": error.public_recovery_hint,
            "request_id": request_id,
            "details": error.details,
        }
    }
    response: HttpResponse
    if request.headers.get("Accept", "").lower().find("application/json") >= 0:
        response = JsonResponse(payload, status=error.status_code)
    else:
        response = render_error_page(request, status=error.status_code, error=error)
    response["X-Error-Code"] = error.code
    return response


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = _request_id(request)
        token = request_id_context.set(request_id)
        request.request_id = request_id  # type: ignore[attr-defined]
        try:
            response = self.get_response(request)
        finally:
            request_id_context.reset(token)
        response["X-Request-ID"] = request_id
        return response


class ErrorHandlingMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            return self.get_response(request)
        except ApplicationError as error:
            logger.warning("Application request failed: %s", error.code)
            return _error_response(request, error)
        except Http404:
            not_found = ResourceNotFoundError("The requested resource was not found.")
            logger.warning("Application request failed: %s", not_found.code)
            return _error_response(request, not_found)
        except PermissionDenied:
            forbidden = ForbiddenError("You do not have permission to perform that action.")
            logger.warning("Application request failed: %s", forbidden.code)
            return _error_response(request, forbidden)
        except Exception:
            logger.exception("Unhandled application request failure")
            if settings.DEBUG:
                raise
            return _error_response(
                request,
                InternalServerError("An unexpected error occurred."),
            )


class DataKeyScopeMiddleware:
    """Makes sure no request leaves an unwrapped key behind it.

    The scope itself is opened lazily, by the first view that needs a key —
    most requests never decrypt anything, and unwrapping for a page that only
    lists dates would be work done for nobody. What cannot be lazy is the
    clearing. A worker thread serves one request after another, and a key left
    in a context variable is a key the next request could read.

    So this closes the scope on the way out, in a ``finally``, whatever
    happened in between.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            return self.get_response(request)
        finally:
            clear_scope()
