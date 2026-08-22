"""Opt-in error reporting with a deliberately small privacy surface."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "amount",
        "authorization",
        "body",
        "card_number",
        "cookie",
        "cookies",
        "counterparty",
        "data",
        "filename",
        "headers",
        "locals",
        "merchant",
        "ocr",
        "ocr_output",
        "ocr_text",
        "password",
        "query_string",
        "raw_output",
        "screenshot",
        "secret",
        "token",
    }
)


def _scrub(value: Any) -> Any:
    if isinstance(value, MutableMapping):
        for key in list(value):
            if str(key).casefold() in SENSITIVE_KEYS:
                del value[key]
            else:
                value[key] = _scrub(value[key])
        return value
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Remove request and financial fields before an event can leave the process."""

    del hint
    return _scrub(event)


def configure_sentry() -> bool:
    """Initialize Sentry only when an operator explicitly supplies a DSN."""

    from django.conf import settings

    dsn = str(getattr(settings, "SENTRY_DSN", "") or "").strip()
    if not dsn:
        return False

    import sentry_sdk

    initializer: Any = sentry_sdk.init
    initializer(
        dsn=dsn,
        environment=getattr(settings, "SENTRY_ENVIRONMENT", "production"),
        send_default_pii=False,
        request_bodies="never",
        with_locals=False,
        before_send=scrub_event,
    )
    return True
