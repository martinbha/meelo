from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from .context import (
    document_id_context,
    job_id_context,
    request_id_context,
    task_id_context,
)

SENSITIVE_FIELD_NAMES = (
    "ocr_text",
    "merchant",
    "counterparty",
    "amount",
    "identifier",
    "account_number",
    "card_number",
    "last_four",
    "screenshot",
    "filename",
    "password",
    "secret",
    "token",
    "authorization",
    # A TOTP device's shared seed is stored in a field called "key", and a
    # recovery code in one called "token". Over-redacting a log line that
    # happened to say "key" costs a reader some context; under-redacting one
    # costs the second factor entirely.
    "key",
    "otp_secret",
    "recovery_code",
    "cookie",
    "cookies",
    "session_key",
    "csrf_token",
    "access_token",
    "refresh_token",
    "api_key",
    "raw_output",
    "approval_code",
)
_SENSITIVE_FIELD_PATTERN = (
    rf"(?:{'|'.join(SENSITIVE_FIELD_NAMES)}|[A-Za-z0-9_]*(?:password|secret|token))"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>\b{_SENSITIVE_FIELD_PATTERN}\b\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^,;\n]*?)"
    r"(?=(?:\s+\b[A-Za-z_][A-Za-z0-9_]*\b\s*[:=])|[,;\n]|$)"
)


def _is_sensitive_field(key: object) -> bool:
    normalized = str(key).lower()
    return normalized in SENSITIVE_FIELD_NAMES or normalized.endswith(
        ("_password", "_secret", "_token")
    )


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_field(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    return _SENSITIVE_ASSIGNMENT.sub(r"\g<prefix>[REDACTED]", value)


def redact_sensitive(value: str) -> str:
    """Remove known financial and credential fields from log text."""

    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return _redact_text(value)
    if isinstance(decoded, str):
        return _redact_text(decoded)
    return json.dumps(_redact_json(decoded), default=str, sort_keys=True)


class RequestContextFilter(logging.Filter):
    """Stamp every line with the identifiers that let two halves be joined.

    The worker and the web process log to different places and fail at different
    times. Without a shared request and task identifier, "why did this document
    never finish" is answered by reading timestamps and guessing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        record.task_id = task_id_context.get()
        record.job_id = job_id_context.get()
        record.document_id = document_id_context.get()
        return True


class SensitiveLogFilter(logging.Filter):
    """Sanitize records before any configured handler renders them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_sensitive(logging.Formatter().formatException(record.exc_info))
        return True


class StructuredFormatter(logging.Formatter):
    """Render one JSON object per log line for ingestion by deployment tools."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive(record.getMessage()),
            "request_id": getattr(record, "request_id", request_id_context.get()),
            "task_id": getattr(record, "task_id", task_id_context.get()),
            "job_id": getattr(record, "job_id", job_id_context.get()),
            "document_id": getattr(record, "document_id", document_id_context.get()),
        }
        metric = getattr(record, "metric", None)
        if isinstance(metric, dict):
            # Metric labels are already allow-listed by apps.core.metrics, so
            # they pass through as structured fields rather than being flattened
            # into a message a dashboard would have to parse back out.
            payload["metric"] = _redact_json(metric)
        if record.exc_info:
            payload["exception"] = redact_sensitive(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, sort_keys=True)
