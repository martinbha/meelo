from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from .context import request_id_context

SENSITIVE_FIELD_NAMES = (
    "ocr_text",
    "merchant",
    "counterparty",
    "amount",
    "identifier",
    "screenshot",
    "filename",
    "password",
    "secret",
    "token",
    "authorization",
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>\b(?:{'|'.join(SENSITIVE_FIELD_NAMES)})\b\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_FIELD_NAMES else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def redact_sensitive(value: str) -> str:
    """Remove known financial and credential fields from log text."""

    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return _SENSITIVE_ASSIGNMENT.sub(r"\g<prefix>[REDACTED]", value)
    return json.dumps(_redact_json(decoded), default=str, sort_keys=True)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
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
        }
        if record.exc_info:
            payload["exception"] = redact_sensitive(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, sort_keys=True)
