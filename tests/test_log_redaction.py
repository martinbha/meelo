from __future__ import annotations

import json
import logging
import sys

import pytest

from apps.core.logging import (
    RequestContextFilter,
    SensitiveLogFilter,
    StructuredFormatter,
    redact_sensitive,
)


@pytest.mark.parametrize(
    ("message", "secrets"),
    [
        ("merchant=스타벅스 강남점", ("스타벅스", "강남점")),
        ("amount: 42900 KRW", ("42900", "KRW")),
        ("cookie=session-cookie", ("session-cookie",)),
        ("api_key=abcdef123456", ("abcdef123456",)),
    ],
)
def test_assignment_redaction_removes_the_whole_sensitive_value(
    message: str, secrets: tuple[str, ...]
) -> None:
    redacted = redact_sensitive(message)

    assert "[REDACTED]" in redacted
    assert all(secret not in redacted for secret in secrets)


def test_exception_text_is_redacted_in_structured_logs() -> None:
    try:
        raise RuntimeError("merchant=스타벅스 amount=42900 cookie=session-cookie")
    except RuntimeError:
        record = logging.LogRecord(
            "apps.test", logging.ERROR, __file__, 1, "pipeline failed", (), sys.exc_info()
        )

    RequestContextFilter().filter(record)
    payload = json.loads(StructuredFormatter().format(record))

    assert "[REDACTED]" in payload["exception"]
    for secret in ("스타벅스", "42900", "session-cookie"):
        assert secret not in payload["exception"]


def test_redaction_filter_sanitizes_a_record_before_handlers_see_it() -> None:
    record = logging.LogRecord(
        "apps.test",
        logging.ERROR,
        __file__,
        1,
        "failed for merchant=%s",
        ("스타벅스",),
        None,
    )

    SensitiveLogFilter().filter(record)

    assert record.getMessage() == "failed for merchant=[REDACTED]"
    assert record.args == ()
