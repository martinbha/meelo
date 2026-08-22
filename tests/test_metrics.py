"""Privacy-safe metrics and correlation (#97, specification 32).

Operational metrics and financial privacy pull in opposite directions: the useful
label is always the specific one, and the specific one is what turns a metrics
pipeline into an unencrypted copy of somebody's finances. These tests hold the
refusal to be structural rather than a convention callers are asked to follow.
"""

from __future__ import annotations

import json
import logging
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core import metrics
from apps.core.context import request_id_context, task_id_context
from apps.core.logging import RequestContextFilter, StructuredFormatter
from apps.core.metrics import MetricError, record, timed
from apps.core.models import WorkerHeartbeat
from apps.processing.models import ProcessingJob


class _Collector(logging.Handler):
    """Capture metric records directly.

    ``caplog`` cannot see these: the ``apps`` logger is configured with
    ``propagate: False`` so structured lines do not also reach the root handler
    in production, which is the behaviour worth keeping.
    """

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, "metric", None)
        if isinstance(payload, dict):
            self.payloads.append(payload)


@pytest.fixture
def emitted() -> Any:
    handler = _Collector()
    logger = logging.getLogger("apps.metrics")
    logger.addHandler(handler)
    try:
        yield handler.payloads
    finally:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# What a metric may carry
# ---------------------------------------------------------------------------


def test_an_unlisted_metric_is_refused() -> None:
    """A metric added ad hoc is one nobody has checked for what it carries."""

    with pytest.raises(MetricError, match="Unknown metric"):
        record("something.invented", document_id="abc")


@pytest.mark.parametrize(
    "label",
    ["merchant", "amount", "ocr_text", "filename", "password", "card_number", "counterparty"],
)
def test_a_value_bearing_label_is_refused(label: str) -> None:
    """Refused by name, so it cannot arrive by being spelled slightly differently."""

    labels: dict[str, Any] = {label: "anything"}

    with pytest.raises(MetricError, match="allow-listed"):
        record(metrics.OCR_FAILED, **labels)


@pytest.mark.parametrize(
    "value",
    ["스타벅스 강남점", "42,900 KRW", "a free text reason", "₩4200", "x" * 65],
)
def test_a_label_that_does_not_look_like_an_identifier_is_refused(value: str) -> None:
    """Free text is how a merchant name reaches a dashboard."""

    with pytest.raises(MetricError, match="identifier"):
        record(metrics.PARSER_FAILED, parser=value)


def test_an_identifier_is_accepted() -> None:
    payload = record(
        metrics.PARSER_SELECTED, parser="toss_bank", source_type="bank_transaction_list"
    )

    assert payload["metric"] == metrics.PARSER_SELECTED
    assert payload["parser"] == "toss_bank"


def test_every_metric_name_is_in_the_registry() -> None:
    """The constants and the allow-list cannot drift apart."""

    declared = {
        value
        for name, value in vars(metrics).items()
        if name.isupper() and isinstance(value, str) and "." in value and name != "MAX_LABEL_LENGTH"
    }

    assert declared == set(metrics.METRIC_NAMES)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_a_metric_carries_the_request_it_came_from() -> None:
    token = request_id_context.set("req-123")
    try:
        payload = record(metrics.UPLOAD_RECEIVED)
    finally:
        request_id_context.reset(token)

    assert payload["request_id"] == "req-123"


def test_a_metric_carries_the_task_when_one_is_running() -> None:
    """Without a shared identifier, "why did this never finish" is guesswork."""

    request = request_id_context.set("req-123")
    task = task_id_context.set("task-456")
    try:
        payload = record(metrics.OCR_FAILED, document_id="doc-1")
    finally:
        task_id_context.reset(task)
        request_id_context.reset(request)

    assert payload["request_id"] == "req-123"
    assert payload["task_id"] == "task-456"
    assert payload["document_id"] == "doc-1"


def test_no_task_label_appears_outside_a_task() -> None:
    payload = record(metrics.UPLOAD_RECEIVED)

    assert "task_id" not in payload


def test_log_lines_carry_both_identifiers() -> None:
    request = request_id_context.set("req-abc")
    task = task_id_context.set("task-def")
    try:
        record_obj = logging.LogRecord(
            "apps.test", logging.INFO, __file__, 1, "processing document", None, None
        )
        RequestContextFilter().filter(record_obj)
        payload = json.loads(StructuredFormatter().format(record_obj))
    finally:
        task_id_context.reset(task)
        request_id_context.reset(request)

    assert payload["request_id"] == "req-abc"
    assert payload["task_id"] == "task-def"


def test_a_metric_reaches_the_log_as_structured_fields() -> None:
    """So a dashboard reads fields rather than parsing them back out of a message."""

    record_obj = logging.LogRecord("apps.metrics", logging.INFO, __file__, 1, "metric", None, None)
    record_obj.metric = {"metric": metrics.QUEUE_DEPTH, "value": 3, "status": "pending"}
    RequestContextFilter().filter(record_obj)

    payload = json.loads(StructuredFormatter().format(record_obj))

    assert payload["metric"]["metric"] == metrics.QUEUE_DEPTH
    assert payload["metric"]["value"] == 3


def test_a_sensitive_key_smuggled_into_a_metric_record_is_redacted() -> None:
    """Belt and braces: the formatter redacts even what the allow-list refused."""

    record_obj = logging.LogRecord("apps.metrics", logging.INFO, __file__, 1, "metric", None, None)
    record_obj.metric = {"metric": metrics.QUEUE_DEPTH, "merchant": "스타벅스"}
    RequestContextFilter().filter(record_obj)

    payload = json.loads(StructuredFormatter().format(record_obj))

    assert payload["metric"]["merchant"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def test_a_timed_block_reports_its_duration_and_outcome(emitted: Any) -> None:
    with timed(metrics.OCR_DURATION, engine="tesseract"):
        pass

    payload = emitted[-1]
    assert payload["metric"] == metrics.OCR_DURATION
    assert payload["outcome"] == "ok"
    assert payload["engine"] == "tesseract"
    assert payload["value"] >= 0


def test_a_failed_block_is_still_timed_and_says_so(emitted: Any) -> None:
    """A duration that mixes fast failures with slow successes is not a number."""

    with pytest.raises(RuntimeError), timed(metrics.OCR_DURATION, engine="paddle"):
        raise RuntimeError("boom")

    payload = emitted[-1]
    assert payload["outcome"] == "error"
    assert payload["metric"] == metrics.OCR_DURATION


# ---------------------------------------------------------------------------
# The status command
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_status_command_reports_queue_and_cleanup_health() -> None:
    out = StringIO()
    call_command("operational_status", stdout=out)
    body = out.getvalue()

    for name in (
        "queue_depth",
        "processing",
        "failed",
        "cleanup_failures",
        "unreviewed_observations",
        "database_latency_ms",
    ):
        assert name in body


@pytest.mark.django_db
def test_the_status_command_emits_machine_readable_output() -> None:
    out = StringIO()
    call_command("operational_status", "--json", stdout=out)

    reading = json.loads(out.getvalue())

    assert reading["queue_depth"] == 0
    assert reading["database_latency_ms"] >= 0


@pytest.mark.django_db
def test_the_status_command_reports_nothing_a_person_owns() -> None:
    """Counts, durations, statuses. Never a merchant, an amount, or a filename."""

    out = StringIO()
    call_command("operational_status", "--json", stdout=out)
    reading = json.loads(out.getvalue())

    # Every reading is a number. There is no field a name or a filename could
    # travel in.
    assert all(isinstance(value, int | float) for value in reading.values())


@pytest.mark.django_db
def test_the_status_command_can_emit_its_readings_as_metrics(emitted: Any) -> None:
    call_command("operational_status", "--emit-metrics", stdout=StringIO())

    names = {payload["metric"] for payload in emitted}

    assert metrics.QUEUE_DEPTH in names
    assert metrics.CLEANUP_FAILED in names
    assert metrics.DATABASE_LATENCY in names


@pytest.mark.django_db
def test_the_status_command_reports_worker_heartbeat_and_thresholds() -> None:
    WorkerHeartbeat.touch("test-worker")
    out = StringIO()

    call_command("operational_status", "--json", stdout=out)

    reading = json.loads(out.getvalue())
    assert reading["worker_available"] == 1
    assert reading["worker_heartbeat_age_seconds"] >= 0
    assert reading["healthy"] == 1


@pytest.mark.django_db
def test_the_status_command_returns_nonzero_when_a_threshold_is_breached() -> None:
    with pytest.raises(CommandError, match="thresholds"):
        call_command("operational_status", "--max-queue-depth", "-1", stdout=StringIO())


@pytest.mark.django_db
def test_queue_claim_records_database_latency(emitted: Any) -> None:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user("metrics-owner@example.com", password="password")
    ProcessingJob.objects.create(
        user=user,
        document_id="00000000-0000-0000-0000-000000000001",
        task_name="test",
    )

    ProcessingJob.claim_next()

    assert any(payload["metric"] == metrics.DATABASE_QUEUE_CLAIM for payload in emitted)


# ---------------------------------------------------------------------------
# The production paths actually emit
# ---------------------------------------------------------------------------


def test_every_metric_has_a_caller() -> None:
    """A metric nothing emits is a claim, not a measurement."""

    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if "metrics.py" not in path.name
    )
    emitted = set(re.findall(r"metrics\.([A-Z_]+)", sources))
    constants = {
        name
        for name, value in vars(metrics).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }

    unused = constants - emitted
    # The rest are wired as the features that produce them land; these are the
    # ones with a caller today.
    assert {
        "UPLOAD_RECEIVED",
        "UPLOAD_REJECTED",
        "OCR_DURATION",
        "OCR_FAILED",
        "QUEUE_DEPTH",
        "CLEANUP_FAILED",
        "DATABASE_LATENCY",
    } <= emitted, sorted(unused)


@pytest.mark.django_db
def test_the_worker_stamps_a_task_identifier(monkeypatch: Any) -> None:
    """So a failure in the worker joins the upload that queued it."""

    from apps.processing.management.commands import process_document_jobs

    seen: list[str] = []

    def capture() -> bool:
        seen.append(task_id_context.get())
        return False

    monkeypatch.setattr(process_document_jobs, "process_one_job", capture)
    call_command("process_document_jobs", "--once", stdout=StringIO())

    assert seen
    assert seen[0] not in {"", "-"}
    # And it is cleared afterwards, so an unrelated log line does not inherit it.
    assert task_id_context.get() == "-"
