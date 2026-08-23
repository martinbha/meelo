from datetime import timedelta

import pytest

from apps.processing.retry import (
    BACKOFF_MAX_SECONDS,
    RETRYABLE_ERROR_CODES,
    TERMINAL_ERROR_CODES,
    is_retryable_error,
    retry_delay,
)


def test_error_code_catalogue_is_disjoint_and_terminal_by_default() -> None:
    assert RETRYABLE_ERROR_CODES.isdisjoint(TERMINAL_ERROR_CODES)
    assert is_retryable_error("OCR_ENGINE_TIMEOUT") is True
    assert is_retryable_error("IMAGE_DECODE_FAILED") is False
    assert is_retryable_error("A_NEW_UNCLASSIFIED_ERROR") is False


def test_backoff_increases_with_jitter_and_is_capped() -> None:
    delays = [retry_delay(attempt, jitter=lambda _low, high: high) for attempt in range(1, 11)]

    assert all(left < right for left, right in zip(delays[:7], delays[1:8], strict=True))
    assert delays[8:] == [timedelta(seconds=BACKOFF_MAX_SECONDS)] * 2
    assert all(delay <= timedelta(seconds=BACKOFF_MAX_SECONDS) for delay in delays)


def test_backoff_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError):
        retry_delay(0)
