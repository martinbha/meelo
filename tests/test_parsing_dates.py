from datetime import UTC, date, datetime

import pytest

from apps.parsing.dates import (
    DateContext,
    DateInference,
    InvalidDateContextError,
    looks_like_date,
    resolve_date,
    resolve_row_dates,
)

SEOUL = "Asia/Seoul"


def context(
    uploaded_at: datetime = datetime(2026, 8, 16, 3, 0, tzinfo=UTC),
    **kwargs: object,
) -> DateContext:
    return DateContext(uploaded_at=uploaded_at, time_zone=SEOUL, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-08-05", date(2026, 8, 5)),
        ("2026.08.05", date(2026, 8, 5)),
        ("2026/8/5", date(2026, 8, 5)),
        ("2026년 8월 5일", date(2026, 8, 5)),
        ("26년 8월 5일", date(2026, 8, 5)),
    ],
)
def test_explicit_full_dates_round_trip(text: str, expected: date) -> None:
    resolved = resolve_date(text, context())

    assert resolved.value == expected
    assert resolved.inference is DateInference.EXPLICIT
    assert resolved.confidence == 1.0
    assert resolved.requires_review is False


def test_relative_dates_resolve_against_the_configured_time_zone() -> None:
    # 2026-08-16T03:00Z is already 2026-08-16 noon in Seoul.
    seoul = resolve_date("오늘", context())
    utc = resolve_date("오늘", DateContext(datetime(2026, 8, 16, 3, 0, tzinfo=UTC), "UTC"))

    assert seoul.value == date(2026, 8, 16)
    assert utc.value == date(2026, 8, 16)
    assert resolve_date("어제", context()).value == date(2026, 8, 15)
    assert seoul.inference is DateInference.RELATIVE_TODAY


def test_relative_dates_cross_the_local_day_boundary() -> None:
    # 2026-08-15T20:00Z is 2026-08-16 05:00 in Seoul, the next local day.
    late = DateContext(datetime(2026, 8, 15, 20, 0, tzinfo=UTC), SEOUL)

    assert resolve_date("오늘", late).value == date(2026, 8, 16)
    assert resolve_date("yesterday", late).value == date(2026, 8, 15)


@pytest.mark.parametrize("text", ["08.05", "08/05", "8월 5일", "8.5"])
def test_missing_year_is_inferred_from_the_upload_date(text: str) -> None:
    resolved = resolve_date(text, context())

    assert resolved.value == date(2026, 8, 5)
    assert resolved.inference is DateInference.UPLOAD_YEAR
    assert resolved.confidence < 1.0
    assert resolved.requires_review is True


def test_missing_year_never_lands_in_the_future() -> None:
    resolved = resolve_date("12.28", context(datetime(2026, 1, 3, 3, 0, tzinfo=UTC)))

    assert resolved.value == date(2025, 12, 28)
    assert resolved.inference is DateInference.UPLOAD_PREVIOUS_YEAR
    assert resolved.requires_review is True


def test_statement_month_wins_over_the_upload_date() -> None:
    resolved = resolve_date("12.28", context(statement_month=date(2024, 12, 1)))

    assert resolved.value == date(2024, 12, 28)
    assert resolved.inference is DateInference.STATEMENT_MONTH_YEAR


def test_statement_month_spans_the_year_boundary() -> None:
    resolved = resolve_date("12.30", context(statement_month=date(2025, 1, 1)))

    assert resolved.value == date(2024, 12, 30)
    assert resolved.inference is DateInference.STATEMENT_MONTH_YEAR


def test_surrounding_dates_are_used_when_no_statement_month_exists() -> None:
    resolved = resolve_date(
        "03.02",
        context(surrounding_dates=(date(2024, 3, 1), date(2024, 3, 4))),
    )

    assert resolved.value == date(2024, 3, 2)
    assert resolved.inference is DateInference.SURROUNDING_ROW_YEAR


def test_explicit_rows_inform_partial_rows_in_the_same_screenshot() -> None:
    resolved = resolve_row_dates(("2024-12-31", "12.30", "오늘"), context())

    assert [item.value for item in resolved] == [
        date(2024, 12, 31),
        date(2024, 12, 30),
        date(2026, 8, 16),
    ]
    assert resolved[1].inference is DateInference.SURROUNDING_ROW_YEAR


def test_impossible_dates_are_rejected_rather_than_guessed() -> None:
    assert resolve_date("2026-02-30", context()).value is None
    assert resolve_date("2026-13-01", context()).value is None
    assert resolve_date("스타벅스", context()).value is None
    assert resolve_date("", context()).value is None


def test_leap_days_resolve_inside_the_inference_window() -> None:
    resolved = resolve_date("02.29", context(datetime(2024, 3, 5, 3, 0, tzinfo=UTC)))

    assert resolved.value == date(2024, 2, 29)


def test_leap_days_outside_the_inference_window_are_left_for_review() -> None:
    resolved = resolve_date("02.29", context(datetime(2027, 3, 5, 3, 0, tzinfo=UTC)))

    assert resolved.value is None
    assert resolved.requires_review is True
    assert "02-29" in resolved.reason


def test_looks_like_date_recognizes_supported_shapes_only() -> None:
    assert looks_like_date("2026-08-05")
    assert looks_like_date("8월 5일")
    assert looks_like_date("어제")
    assert not looks_like_date("42900 KRW")
    assert not looks_like_date("스타벅스")


def test_naive_upload_timestamps_are_rejected() -> None:
    with pytest.raises(InvalidDateContextError):
        DateContext(datetime(2026, 8, 16, 12, 0), SEOUL)

    with pytest.raises(InvalidDateContextError):
        DateContext(datetime(2026, 8, 16, 12, 0, tzinfo=UTC), "Mars/Olympus")
