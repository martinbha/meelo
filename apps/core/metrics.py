"""Measuring the system without measuring the user.

Operational metrics and financial privacy pull in opposite directions. The useful
label is always the specific one — *which* merchant is slow to parse, *what*
amount failed to post — and that is exactly the label that turns a metrics
pipeline into an unencrypted copy of somebody's finances, sitting wherever metrics
go and outliving whatever retention the database has.

So this module refuses those labels rather than trusting callers to avoid them
(specification 32). Two rules, both enforced:

- **Label names come from a fixed set.** A name nobody allow-listed is refused,
  so ``merchant`` cannot arrive by being spelled slightly differently.
- **Label values must be identifiers or short enumerations.** A UUID, a status, a
  parser name, a boolean. Not free text, and never a number that could be money.

What is left is enough to run the system: counts, durations, statuses, and the
identifiers needed to join a failure in the worker to the request that caused it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .context import request_id_context, task_id_context

logger = logging.getLogger("apps.metrics")

#: Every metric this system emits. A fixed list, because a metric added ad hoc is
#: one nobody has checked for what it carries.
UPLOAD_RECEIVED = "upload.received"
UPLOAD_REJECTED = "upload.rejected"
OCR_DURATION = "ocr.duration_ms"
OCR_FAILED = "ocr.failed"
PARSER_SELECTED = "parser.selected"
PARSER_ROWS = "parser.rows"
PARSER_FAILED = "parser.failed"
OBSERVATION_CORRECTED = "review.corrected"
OBSERVATION_DISAGREEMENT = "review.amount_disagreement"
MATCH_PROPOSED = "reconciliation.proposed"
MATCH_CONFIRMED = "reconciliation.confirmed"
MATCH_REJECTED = "reconciliation.rejected"
QUEUE_DEPTH = "queue.depth"
CLEANUP_FAILED = "cleanup.failed"
DATABASE_LATENCY = "database.latency_ms"

METRIC_NAMES: frozenset[str] = frozenset(
    {
        UPLOAD_RECEIVED,
        UPLOAD_REJECTED,
        OCR_DURATION,
        OCR_FAILED,
        PARSER_SELECTED,
        PARSER_ROWS,
        PARSER_FAILED,
        OBSERVATION_CORRECTED,
        OBSERVATION_DISAGREEMENT,
        MATCH_PROPOSED,
        MATCH_CONFIRMED,
        MATCH_REJECTED,
        QUEUE_DEPTH,
        CLEANUP_FAILED,
        DATABASE_LATENCY,
    }
)

#: Labels a metric may carry. Every one of these is an identifier, a status, or a
#: name of something in the codebase — never a value read off a screenshot.
ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "document_id",
        "observation_id",
        "match_id",
        "task_id",
        "request_id",
        "engine",
        "parser",
        "source_type",
        "status",
        "reason",
        "match_type",
        "filter",
        "outcome",
        "error_code",
    }
)

#: What a label value may look like. Identifiers, dotted names, and short codes.
#: Anything with a space, a currency symbol, or non-Latin text is refused — those
#: are how a merchant name or an amount would arrive.
_LABEL_VALUE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")

#: Reasons and error codes are the one place a short phrase is useful, so they
#: allow underscores and hyphens only, which excludes anything OCR produced.
MAX_LABEL_LENGTH = 64


class MetricError(ValueError):
    """A metric cannot be emitted as described."""


def _check(name: str, labels: Mapping[str, Any]) -> dict[str, str]:
    if name not in METRIC_NAMES:
        raise MetricError(f"Unknown metric: {name!r}. Add it to METRIC_NAMES first.")
    checked: dict[str, str] = {}
    for key, value in labels.items():
        if key not in ALLOWED_LABELS:
            raise MetricError(
                f"Label {key!r} is not allow-listed. Metrics carry identifiers and "
                f"statuses, never values."
            )
        text = str(value)
        if not _LABEL_VALUE.match(text):
            raise MetricError(
                f"Label {key!r} has a value that does not look like an identifier. "
                f"Free text is how a merchant name reaches a dashboard."
            )
        checked[key] = text
    return checked


def record(name: str, *, value: float = 1, **labels: Any) -> dict[str, Any]:
    """Emit one measurement, refusing anything that could carry a value.

    Returns the record it logged, so a caller — or a test — can assert on exactly
    what left the process.
    """

    checked = _check(name, labels)
    checked.setdefault("request_id", request_id_context.get())
    task = task_id_context.get()
    if task and task != "-":
        checked.setdefault("task_id", task)
    payload = {"metric": name, "value": value, **checked}
    logger.info("metric", extra={"metric": payload})
    return payload


@contextmanager
def timed(name: str, **labels: Any) -> Iterator[dict[str, Any]]:
    """Time a block and emit its duration, whether or not it succeeded.

    The outcome label is set for you: a duration without knowing whether the work
    finished is a number that quietly mixes fast failures with slow successes.
    """

    started = time.perf_counter()
    state: dict[str, Any] = {"outcome": "ok"}
    try:
        yield state
    except Exception:
        state["outcome"] = "error"
        raise
    finally:
        record(
            name,
            value=round((time.perf_counter() - started) * 1000, 3),
            **{**labels, "outcome": state["outcome"]},
        )
