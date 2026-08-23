from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from uuid import uuid4

import pytest

from apps.core.logging import (
    RequestContextFilter,
    SensitiveLogFilter,
    StructuredFormatter,
    redact_sensitive,
)
from apps.processing.models import ProcessingJob
from apps.processing.services import JOB_HANDLERS, process_one_job
from tests.factories import make_user


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


@pytest.mark.django_db
def test_worker_exception_path_never_emits_sensitive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_user(email="log-redaction-worker@example.com")

    def failing_handler(job: ProcessingJob) -> None:
        del job
        raise RuntimeError("merchant=스타벅스 amount=42900 cookie=session-cookie")

    monkeypatch.setitem(JOB_HANDLERS, "log-redaction-test", failing_handler)
    ProcessingJob.objects.create(
        user=owner,
        document_id=uuid4(),
        task_name="log-redaction-test",
    )

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveLogFilter())
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger("apps.processing.services")
    logger.addHandler(handler)
    try:
        assert process_one_job() is True
    finally:
        logger.removeHandler(handler)
        handler.close()

    output = stream.getvalue()
    assert "[REDACTED]" in output
    for secret in ("스타벅스", "42900", "session-cookie"):
        assert secret not in output
