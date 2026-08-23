from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta

from apps.core.errors import ERROR_CATALOGUE

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 300.0
BACKOFF_JITTER_FRACTION = 0.25

# Unknown codes are terminal by default. A retry policy that treats an
# unrecognised failure as transient can turn a programming error into an
# infinite dependency hammer, so adding a retryable code must be deliberate.
RETRYABLE_ERROR_CODES = frozenset(
    code for code, definition in ERROR_CATALOGUE.items() if definition.retryable
)
TERMINAL_ERROR_CODES = frozenset(
    code for code, definition in ERROR_CATALOGUE.items() if not definition.retryable
)

if RETRYABLE_ERROR_CODES & TERMINAL_ERROR_CODES:  # pragma: no cover - import guard
    raise RuntimeError("Retryable and terminal error-code sets must be disjoint.")


def is_retryable_error(code: str) -> bool:
    """Return the explicit retry decision for an error code."""

    normalized = code.strip().upper()
    if normalized in TERMINAL_ERROR_CODES:
        return False
    return normalized in RETRYABLE_ERROR_CODES


def retry_delay(
    attempt_count: int,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> timedelta:
    """Return a bounded exponential delay with additive positive jitter."""

    if attempt_count < 1:
        raise ValueError("Attempt count must be positive.")
    base = min(
        BACKOFF_MAX_SECONDS,
        BACKOFF_INITIAL_SECONDS * (2 ** (attempt_count - 1)),
    )
    jitter_limit = min(
        base * BACKOFF_JITTER_FRACTION,
        max(BACKOFF_MAX_SECONDS - base, 0.0),
    )
    jitter_seconds = min(max(jitter(0.0, jitter_limit), 0.0), jitter_limit)
    return timedelta(seconds=min(base + jitter_seconds, BACKOFF_MAX_SECONDS))
