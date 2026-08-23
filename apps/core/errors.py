from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """The safe public meaning of one persisted application error code."""

    message: str
    recovery_hint: str
    status_code: int = 400
    retryable: bool = False


def _definition(
    message: str,
    recovery_hint: str,
    *,
    status_code: int = 400,
    retryable: bool = False,
) -> ErrorDefinition:
    return ErrorDefinition(message, recovery_hint, status_code, retryable)


# This is the single public catalogue for specification section 27. The text
# deliberately describes the action a person can take without repeating an
# exception, a path, a query, or any data from the failed request.
ERROR_CATALOGUE: Final[Mapping[str, ErrorDefinition]] = MappingProxyType(
    {
        "APPLICATION_ERROR": _definition(
            "The request could not be completed.", "Try again or contact an operator."
        ),
        "UNKNOWN_ERROR": _definition(
            "Something went wrong while completing the request.",
            "Try again. If the problem continues, contact an operator.",
            status_code=500,
        ),
        "INVALID_REQUEST": _definition(
            "The request could not be accepted.", "Check the submitted information and try again."
        ),
        "RESOURCE_NOT_FOUND": _definition(
            "The requested item could not be found.",
            "Return to the previous page and try again.",
            status_code=404,
        ),
        "FORBIDDEN": _definition(
            "You do not have permission to perform that action.",
            "Sign in with the account that owns the item.",
            status_code=403,
        ),
        "CONFLICT": _definition(
            "The item changed before the action completed.",
            "Reload the page and try again.",
            status_code=409,
        ),
        "INTERNAL_SERVER_ERROR": _definition(
            "An unexpected error occurred.",
            "Try again. If the problem continues, contact an operator.",
            status_code=500,
        ),
        "DUPLICATE_UPLOAD": _definition(
            "This screenshot was already uploaded.",
            "Open the existing upload or choose a different screenshot.",
        ),
        "FILE_TOO_LARGE": _definition(
            "The screenshot is larger than the allowed limit.",
            "Choose a smaller screenshot and try again.",
        ),
        "INVALID_FILE_TYPE": _definition(
            "The screenshot format is not supported.", "Use a PNG, JPEG, or WebP screenshot."
        ),
        "IMAGE_DECODE_FAILED": _definition(
            "The screenshot could not be read.",
            "Export the screenshot again and upload the new copy.",
        ),
        "IMAGE_DIMENSIONS_TOO_LARGE": _definition(
            "The screenshot dimensions exceed the safe limit.", "Crop the screenshot and try again."
        ),
        "TEMP_STORAGE_FAILED": _definition(
            "The screenshot could not be stored safely.",
            "Try again. If the problem continues, contact an operator.",
            retryable=True,
        ),
        "CLEANUP_FAILED": _definition(
            "The temporary screenshot could not be removed.",
            "Contact an operator to check storage cleanup.",
        ),
        "DOCUMENT_NOT_FOUND": _definition(
            "The document could not be found.",
            "Return to the upload list and try again.",
            status_code=404,
        ),
        "DOCUMENT_PROCESSING_FAILED": _definition(
            "The document could not be processed.", "Try processing the screenshot again."
        ),
        "TEMP_PATH_INVALID": _definition(
            "The stored screenshot is unavailable.", "Upload the screenshot again."
        ),
        "TEMP_FILE_MISSING": _definition(
            "The stored screenshot is unavailable.", "Upload the screenshot again."
        ),
        "UNSUPPORTED_TASK": _definition(
            "The processing request is not supported by this worker.",
            "Contact an operator to update the application containers.",
        ),
        "UNHANDLED_ERROR": _definition(
            "The document could not be processed.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "TASK_TIMEOUT": _definition(
            "Document processing took too long.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "OCR_CONFIGURATION_INVALID": _definition(
            "Text recognition is not configured for this screenshot.",
            "Contact an operator to check the recognition configuration.",
        ),
        "LANGUAGE_PACK_MISSING": _definition(
            "Text recognition could not load its language data.",
            "Contact an operator to install the required language data.",
        ),
        "OCR_ENGINE_FAILED": _definition(
            "Text recognition could not read the screenshot.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "OCR_ENGINE_CRASHED": _definition(
            "Text recognition stopped unexpectedly.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "OCR_ENGINE_TIMEOUT": _definition(
            "Text recognition took too long.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "OCR_ALL_ENGINES_FAILED": _definition(
            "Text recognition could not read the screenshot.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "OCR_PARSE_HANDOFF_FAILED": _definition(
            "The recognized text could not be prepared for review.",
            "Try processing the screenshot again.",
        ),
        "OCR_TIMEOUT": _definition(
            "Text recognition took too long.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "ENGINE_TIMEOUT": _definition(
            "Text recognition took too long.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "PADDLEOCR_FAILED": _definition(
            "One text-recognition engine could not read the screenshot.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "TESSERACT_FAILED": _definition(
            "One text-recognition engine could not read the screenshot.",
            "Try processing the screenshot again.",
            retryable=True,
        ),
        "NO_TEXT_DETECTED": _definition(
            "No readable text was found in the screenshot.",
            "Upload a clearer or uncropped screenshot.",
        ),
        "PARSER_FAILED": _definition(
            "The screenshot format could not be interpreted.",
            "Upload a supported screenshot and try again.",
        ),
        "PARSER_NOT_FOUND": _definition(
            "This screenshot format is not supported yet.",
            "Upload a supported screenshot or contact an operator.",
        ),
        "RETRYABLE_ERROR": _definition(
            "The document could not be processed yet.",
            "The system will try again automatically.",
            retryable=True,
        ),
        "PERMANENT_ERROR": _definition(
            "The document could not be processed.",
            "Upload a new screenshot or contact an operator.",
        ),
        "DATABASE_CONNECTION_FAILED": _definition(
            "The service is temporarily unavailable.",
            "Try again shortly.",
            status_code=503,
            retryable=True,
        ),
        "DATABASE_POOL_EXHAUSTED": _definition(
            "The service is temporarily busy.",
            "Try again shortly.",
            status_code=503,
            retryable=True,
        ),
        "DATABASE_WRITE_FAILED": _definition(
            "The change could not be saved.", "Try again shortly.", retryable=True
        ),
        "DECRYPTION_FAILED": _definition(
            "The protected data could not be read.",
            "Try again. If the problem continues, contact an operator.",
            retryable=True,
        ),
        "ENCRYPTION_FAILED": _definition(
            "The protected data could not be saved.",
            "Try again. If the problem continues, contact an operator.",
            retryable=True,
        ),
        "OBSERVATION_IMPORT_FAILED": _definition(
            "The recognized rows could not be prepared for review.",
            "Try processing the screenshot again.",
        ),
        "OBSERVATION_ACTION_INVALID": _definition(
            "That review action is not available for this row.",
            "Reload the review queue and try again.",
        ),
        "REPROCESS_NOT_ALLOWED": _definition(
            "This screenshot cannot be processed again.",
            "Upload the screenshot again if a new result is needed.",
        ),
        "RECONCILIATION_INVALID": _definition(
            "That reconciliation action cannot be completed.",
            "Reload the reconciliation queue and try again.",
        ),
        "CURRENCY_MISMATCH": _definition(
            "The amounts use different currencies.", "Choose values with the same currency."
        ),
        "INVALID_CURRENCY": _definition(
            "The currency is not supported.", "Choose a supported currency and try again."
        ),
        "INVALID_MONEY": _definition(
            "The amount is not valid.", "Enter a whole amount in the smallest currency unit."
        ),
        "INVALID_DATE": _definition("The date is not valid.", "Enter a valid transaction date."),
        "INVALID_DATE_CONTEXT": _definition(
            "The date could not be resolved.", "Correct the date in the review form."
        ),
        "BLIND_INDEX_FAILED": _definition(
            "The protected search value could not be prepared.",
            "Try again. If the problem continues, contact an operator.",
        ),
        "INVALID_CIPHERTEXT": _definition(
            "The protected data could not be verified.",
            "Contact an operator to check the protected data.",
        ),
        "MALFORMED_PAYLOAD": _definition(
            "The protected value is not valid.",
            "Try again. If the problem continues, contact an operator.",
        ),
        "KEY_MANAGEMENT_FAILED": _definition(
            "Protected data is temporarily unavailable.",
            "Contact an operator to check key configuration.",
        ),
        "BACKUP_FAILED": _definition(
            "The backup could not be created or read.", "Check the backup settings and try again."
        ),
        "EXPORT_FAILED": _definition(
            "The export could not be created or read.", "Check the export request and try again."
        ),
    }
)


def definition_for(code: str | None) -> ErrorDefinition:
    """Return a catalogue entry, safely handling blank and unknown codes."""

    normalized = str(code or "").strip().upper()
    return ERROR_CATALOGUE.get(normalized, ERROR_CATALOGUE["UNKNOWN_ERROR"])


def public_error_message(code: str | None) -> str:
    return definition_for(code).message


def recovery_hint(code: str | None) -> str:
    return definition_for(code).recovery_hint


class ApplicationError(Exception):
    """An expected application failure with safe public rendering."""

    code = "APPLICATION_ERROR"
    status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    @property
    def public_message(self) -> str:
        return public_error_message(self.code)

    @property
    def public_recovery_hint(self) -> str:
        return recovery_hint(self.code)


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
