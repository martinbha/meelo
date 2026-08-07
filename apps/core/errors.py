from __future__ import annotations

from typing import Any


class ApplicationError(Exception):
    """An expected, safe-to-expose application failure."""

    code = "APPLICATION_ERROR"
    status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidRequestError(ApplicationError):
    code = "INVALID_REQUEST"
    status_code = 400


class ResourceNotFoundError(ApplicationError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class ForbiddenError(ApplicationError):
    code = "FORBIDDEN"
    status_code = 403


class ConflictError(ApplicationError):
    code = "CONFLICT"
    status_code = 409


class InternalServerError(ApplicationError):
    code = "INTERNAL_SERVER_ERROR"
    status_code = 500
