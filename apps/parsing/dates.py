"""Resolve Korean, relative, and partial dates found in screenshot text.

Screenshots rarely spell out a full date. Rows show ``08.05``, ``8월 5일``, or
``오늘``, and the missing information has to be inferred from the statement
month, the surrounding rows, or the upload time. Every resolution records why a
value was chosen so review can weigh inferred dates differently from explicit
ones.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings

FULL_DATE_RE = re.compile(
    r"^(?P<year>\d{4})\s*[-./]\s*(?P<month>\d{1,2})\s*[-./]\s*(?P<day>\d{1,2})\.?$"
)
KOREAN_FULL_DATE_RE = re.compile(
    r"^(?P<year>\d{2,4})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일?$"
)
KOREAN_PARTIAL_DATE_RE = re.compile(r"^(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일?$")
PARTIAL_DATE_RE = re.compile(r"^(?P<month>\d{1,2})\s*[-./]\s*(?P<day>\d{1,2})\.?$")

TODAY_WORDS = frozenset({"오늘", "today", "금일"})
YESTERDAY_WORDS = frozenset({"어제", "어저께", "yesterday", "전일"})

#: Partial dates may sit slightly ahead of the upload moment because the phone
#: clock, the bank server, and the container may disagree by a few hours.
FUTURE_GRACE = timedelta(days=1)


class DateInference(StrEnum):
    """Why a resolved date holds the value it does."""

    EXPLICIT = "explicit"
    RELATIVE_TODAY = "relative_today"
    RELATIVE_YESTERDAY = "relative_yesterday"
    STATEMENT_MONTH_YEAR = "statement_month_year"
    SURROUNDING_ROW_YEAR = "surrounding_row_year"
    UPLOAD_YEAR = "upload_year"
    UPLOAD_PREVIOUS_YEAR = "upload_previous_year"


#: Confidence assigned to each inference path. Explicit dates round-trip
#: exactly; every inferred year is reviewable.
INFERENCE_CONFIDENCE: dict[DateInference, float] = {
    DateInference.EXPLICIT: 1.0,
    DateInference.RELATIVE_TODAY: 0.9,
    DateInference.RELATIVE_YESTERDAY: 0.9,
    DateInference.STATEMENT_MONTH_YEAR: 0.8,
    DateInference.SURROUNDING_ROW_YEAR: 0.75,
    DateInference.UPLOAD_YEAR: 0.6,
    DateInference.UPLOAD_PREVIOUS_YEAR: 0.5,
}

#: Resolutions at or below this confidence must be confirmed by a human.
REVIEW_CONFIDENCE_THRESHOLD = 0.8


class InvalidDateContextError(ValueError):
    """The supplied resolution context cannot be used."""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidDateContextError(f"Unknown time zone: {name}") from exc


@dataclass(frozen=True, slots=True)
class DateContext:
    """Everything known about a document that helps date a single row."""

    uploaded_at: datetime
    time_zone: str = ""
    statement_month: date | None = None
    surrounding_dates: tuple[date, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.uploaded_at.tzinfo is None:
            raise InvalidDateContextError("Upload timestamps must be timezone aware.")
        name = self.time_zone or str(settings.TIME_ZONE)
        _zone(name)
        object.__setattr__(self, "time_zone", name)
        object.__setattr__(self, "surrounding_dates", tuple(self.surrounding_dates))

    @property
    def local_today(self) -> date:
        """The upload date as seen in the user's configured local time zone."""

        return self.uploaded_at.astimezone(_zone(self.time_zone)).date()


@dataclass(frozen=True, slots=True)
class ResolvedDate:
    """A date candidate together with the reason it was chosen."""

    value: date | None
    inference: DateInference | None
    confidence: float
    reason: str

    @property
    def requires_review(self) -> bool:
        return self.value is None or self.confidence <= REVIEW_CONFIDENCE_THRESHOLD


UNRESOLVED = ResolvedDate(None, None, 0.0, "no date pattern matched")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _expand_year(year: str) -> int:
    number = int(year)
    return number if len(year) == 4 else 2000 + number


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _anchor(context: DateContext) -> tuple[date, DateInference, bool]:
    """Pick the reference point used to fill in a missing year.

    Returns the anchor date, the inference it implies, and whether dates after
    the anchor are plausible. Statement months and surrounding rows describe
    real transaction dates, so later dates are expected there; the upload time
    is an upper bound instead.
    """

    if context.statement_month is not None:
        month_middle = context.statement_month.replace(day=15)
        return month_middle, DateInference.STATEMENT_MONTH_YEAR, True
    if context.surrounding_dates:
        ordinals = sorted(value.toordinal() for value in context.surrounding_dates)
        return (
            date.fromordinal(int(median(ordinals))),
            DateInference.SURROUNDING_ROW_YEAR,
            True,
        )
    return context.local_today, DateInference.UPLOAD_YEAR, False


def _infer_year(month: int, day: int, context: DateContext) -> ResolvedDate:
    anchor, inference, allow_future = _anchor(context)
    limit = context.local_today + FUTURE_GRACE
    candidates: list[date] = []
    for offset in (-1, 0, 1):
        candidate = _build(anchor.year + offset, month, day)
        if candidate is None:
            continue
        if not allow_future and candidate > limit:
            continue
        candidates.append(candidate)
    if not candidates:
        return ResolvedDate(
            None,
            None,
            0.0,
            f"no valid year could be inferred for {month:02d}-{day:02d}",
        )
    chosen = min(candidates, key=lambda value: (abs(value - anchor), value))
    if inference is DateInference.UPLOAD_YEAR and chosen.year != anchor.year:
        inference = DateInference.UPLOAD_PREVIOUS_YEAR
    return ResolvedDate(
        chosen,
        inference,
        INFERENCE_CONFIDENCE[inference],
        f"year {chosen.year} inferred from {inference.value}",
    )


def _explicit(value: date) -> ResolvedDate:
    return ResolvedDate(
        value,
        DateInference.EXPLICIT,
        INFERENCE_CONFIDENCE[DateInference.EXPLICIT],
        "explicit full date",
    )


def resolve_date(text: str, context: DateContext) -> ResolvedDate:
    """Resolve one date-like token against the surrounding document context."""

    cleaned = _clean(text)
    if not cleaned:
        return UNRESOLVED
    folded = cleaned.casefold()
    if folded in TODAY_WORDS:
        return ResolvedDate(
            context.local_today,
            DateInference.RELATIVE_TODAY,
            INFERENCE_CONFIDENCE[DateInference.RELATIVE_TODAY],
            f"'{cleaned}' resolved in {context.time_zone}",
        )
    if folded in YESTERDAY_WORDS:
        return ResolvedDate(
            context.local_today - timedelta(days=1),
            DateInference.RELATIVE_YESTERDAY,
            INFERENCE_CONFIDENCE[DateInference.RELATIVE_YESTERDAY],
            f"'{cleaned}' resolved in {context.time_zone}",
        )

    for pattern in (FULL_DATE_RE, KOREAN_FULL_DATE_RE):
        match = pattern.fullmatch(cleaned)
        if match is None:
            continue
        value = _build(
            _expand_year(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
        if value is None:
            return ResolvedDate(None, None, 0.0, f"'{cleaned}' is not a real calendar date")
        return _explicit(value)

    for pattern in (KOREAN_PARTIAL_DATE_RE, PARTIAL_DATE_RE):
        match = pattern.fullmatch(cleaned)
        if match is None:
            continue
        return _infer_year(int(match.group("month")), int(match.group("day")), context)

    return UNRESOLVED


def looks_like_date(text: str) -> bool:
    """Report whether a token is shaped like any supported date form."""

    cleaned = _clean(text)
    folded = cleaned.casefold()
    if folded in TODAY_WORDS or folded in YESTERDAY_WORDS:
        return True
    return any(
        pattern.fullmatch(cleaned) is not None
        for pattern in (
            FULL_DATE_RE,
            KOREAN_FULL_DATE_RE,
            KOREAN_PARTIAL_DATE_RE,
            PARTIAL_DATE_RE,
        )
    )


def resolve_row_dates(texts: Sequence[str], context: DateContext) -> tuple[ResolvedDate, ...]:
    """Resolve several tokens, letting confidently dated rows inform the rest.

    Explicit dates found in the first pass are added to the surrounding-row
    context so partial dates in the same screenshot inherit their year.
    """

    first_pass = tuple(resolve_date(text, context) for text in texts)
    explicit = tuple(
        resolved.value
        for resolved in first_pass
        if resolved.value is not None and resolved.inference is DateInference.EXPLICIT
    )
    if not explicit or context.statement_month is not None:
        return first_pass
    enriched = DateContext(
        uploaded_at=context.uploaded_at,
        time_zone=context.time_zone,
        statement_month=context.statement_month,
        surrounding_dates=context.surrounding_dates + explicit,
    )
    return tuple(
        resolved if resolved.inference is DateInference.EXPLICIT else resolve_date(text, enriched)
        for text, resolved in zip(texts, first_pass, strict=True)
    )
