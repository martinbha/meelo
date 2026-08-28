"""Run institution parsers against sanitized fixtures and score the results.

Specification section 31.3 requires every parser change to run against all
fixture screenshots for that institution, tracking amount accuracy, date
accuracy, merchant accuracy, and the missed and false transaction rates. This
module loads the fixtures, drives the real registry, and reports those numbers.

Fixtures store raw OCR text. The harness normalizes it through
:func:`apps.ocr.normalization.normalize_ocr_text` so the parsers see exactly
what the OCR pipeline hands them.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from apps.ocr.contracts import BoundingBox
from apps.ocr.normalization import normalize_ocr_text

from .contracts import DocumentMetadata, NormalizedToken, ParsedObservation
from .registry import ParserRegistry, ParserSelection

DEFAULT_TOKEN_CONFIDENCE = 0.95


class ParserFixtureError(ValueError):
    """A fixture file cannot be loaded."""


@dataclass(frozen=True, slots=True)
class ExpectedObservation:
    """One transaction a fixture screenshot is known to contain."""

    occurred_on: date | None = None
    merchant: str | None = None
    counterparty: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    direction: str | None = None
    instrument_suffix: str | None = None
    balance_after_minor: int | None = None
    approval_code: str | None = None
    installment_months: int | None = None
    is_settlement: bool = False


@dataclass(frozen=True, slots=True)
class ParserFixtureCase:
    """A sanitized screenshot expressed as tokens plus its expected output."""

    name: str
    parser: str
    institution: str
    document: DocumentMetadata
    tokens: tuple[NormalizedToken, ...]
    expected: tuple[ExpectedObservation, ...]
    quality: str = "standard"
    minimum_confidence: float = 0.0
    maximum_confidence: float = 1.0
    expected_source_type: str | None = None


@dataclass(frozen=True, slots=True)
class ParserMetrics:
    """Accuracy for one fixture case, in the terms the specification names."""

    name: str
    parser: str
    institution: str
    selected_parser: str
    support_score: float
    detected_source_type: str
    expected_count: int
    observed_count: int
    compared: int
    amount_matches: int
    date_matches: int
    merchant_matches: int
    direction_matches: int
    metadata_matches: int
    missed: int
    false_positives: int
    mismatches: tuple[str, ...] = ()

    def _rate(self, matches: int) -> float:
        return matches / self.compared if self.compared else 1.0

    @property
    def amount_accuracy(self) -> float:
        return self._rate(self.amount_matches)

    @property
    def date_accuracy(self) -> float:
        return self._rate(self.date_matches)

    @property
    def merchant_accuracy(self) -> float:
        return self._rate(self.merchant_matches)

    @property
    def direction_accuracy(self) -> float:
        return self._rate(self.direction_matches)

    @property
    def metadata_accuracy(self) -> float:
        return self._rate(self.metadata_matches)

    @property
    def missed_rate(self) -> float:
        return self.missed / self.expected_count if self.expected_count else 0.0

    @property
    def false_rate(self) -> float:
        return self.false_positives / self.observed_count if self.observed_count else 0.0

    @property
    def is_clean(self) -> bool:
        return not self.mismatches and self.missed == 0 and self.false_positives == 0


@dataclass(frozen=True, slots=True)
class AccuracyAggregate:
    """Weighted accuracy totals for a report group."""

    fixtures: int
    expected_count: int
    observed_count: int
    compared: int
    amount_matches: int
    date_matches: int
    merchant_matches: int
    missed: int
    false_positives: int

    def _accuracy(self, matches: int) -> float:
        return matches / self.compared if self.compared else 1.0

    @property
    def amount_accuracy(self) -> float:
        return self._accuracy(self.amount_matches)

    @property
    def date_accuracy(self) -> float:
        return self._accuracy(self.date_matches)

    @property
    def merchant_accuracy(self) -> float:
        return self._accuracy(self.merchant_matches)

    @property
    def missed_rate(self) -> float:
        return self.missed / self.expected_count if self.expected_count else 0.0

    @property
    def false_rate(self) -> float:
        return self.false_positives / self.observed_count if self.observed_count else 0.0

    def as_dict(self) -> dict[str, int | float]:
        """Return stable report fields without run-specific metadata."""
        return {
            "amount_accuracy": self.amount_accuracy,
            "compared": self.compared,
            "date_accuracy": self.date_accuracy,
            "expected_count": self.expected_count,
            "false_positives": self.false_positives,
            "false_rate": self.false_rate,
            "fixtures": self.fixtures,
            "merchant_accuracy": self.merchant_accuracy,
            "missed": self.missed,
            "missed_rate": self.missed_rate,
            "observed_count": self.observed_count,
        }


@dataclass(frozen=True, slots=True)
class ParserAccuracyReport:
    """Deterministic aggregates at every required reporting level."""

    overall: AccuracyAggregate
    by_parser: Mapping[str, AccuracyAggregate]
    by_institution: Mapping[str, AccuracyAggregate]

    def as_dict(self) -> dict[str, object]:
        return {
            "by_institution": {
                name: aggregate.as_dict() for name, aggregate in sorted(self.by_institution.items())
            },
            "by_parser": {
                name: aggregate.as_dict() for name, aggregate in sorted(self.by_parser.items())
            },
            "overall": self.overall.as_dict(),
            "schema_version": 1,
        }

    def to_json(self) -> str:
        """Render a byte-stable machine-readable report."""
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParserFixtureError(f"Expected an ISO date string, got {value!r}.")
    return date.fromisoformat(value)


def _document(payload: Mapping[str, Any]) -> DocumentMetadata:
    uploaded_at_raw = payload.get("uploaded_at")
    uploaded_at: datetime | None = None
    if isinstance(uploaded_at_raw, str):
        parsed = datetime.fromisoformat(uploaded_at_raw)
        uploaded_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return DocumentMetadata(
        source_type=str(payload.get("source_type", "unknown")),
        width=int(payload["width"]) if payload.get("width") is not None else None,
        height=int(payload["height"]) if payload.get("height") is not None else None,
        institution_hint=(
            str(payload["institution_hint"])
            if payload.get("institution_hint") is not None
            else None
        ),
        uploaded_at=uploaded_at,
        time_zone=str(payload.get("time_zone", "")),
        statement_month=_parse_date(payload.get("statement_month")),
        instrument_type=(
            str(payload["instrument_type"]) if payload.get("instrument_type") is not None else None
        ),
    )


def _tokens(payload: Sequence[Mapping[str, Any]]) -> tuple[NormalizedToken, ...]:
    tokens: list[NormalizedToken] = []
    for sequence, item in enumerate(payload):
        bounds = item.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise ParserFixtureError(f"Token {sequence} needs four bounding-box coordinates.")
        confidence = float(item.get("confidence", DEFAULT_TOKEN_CONFIDENCE))
        tokens.append(
            NormalizedToken(
                text=normalize_ocr_text(str(item["text"])),
                confidence=confidence,
                bounding_box=BoundingBox(*(int(edge) for edge in bounds)),
                sequence=sequence,
                source_engines=("fixture",),
            )
        )
    return tuple(tokens)


def _expected(payload: Sequence[Mapping[str, Any]]) -> tuple[ExpectedObservation, ...]:
    return tuple(
        ExpectedObservation(
            occurred_on=_parse_date(item.get("date")),
            merchant=item.get("merchant"),
            counterparty=item.get("counterparty"),
            amount_minor=item.get("amount_minor"),
            currency=item.get("currency"),
            direction=item.get("direction"),
            instrument_suffix=item.get("instrument_suffix"),
            balance_after_minor=item.get("balance_after_minor"),
            approval_code=item.get("approval_code"),
            installment_months=item.get("installment_months"),
            is_settlement=bool(item.get("is_settlement", False)),
        )
        for item in payload
    )


def load_parser_fixtures(
    root: Path, *, institution: str | None = None
) -> tuple[ParserFixtureCase, ...]:
    """Load every fixture under ``root``, optionally for one institution only."""

    resolved = root.resolve()
    pattern = f"{institution}/*.json" if institution else "*/*.json"
    cases: list[ParserFixtureCase] = []
    for path in sorted(resolved.glob(pattern)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            cases.append(
                ParserFixtureCase(
                    name=str(payload["name"]),
                    parser=str(payload["parser"]),
                    institution=path.parent.name,
                    document=_document(payload.get("document", {})),
                    tokens=_tokens(payload.get("tokens", [])),
                    expected=_expected(payload.get("expected", [])),
                    quality=str(payload.get("quality", "standard")),
                    minimum_confidence=float(payload.get("minimum_confidence", 0.0)),
                    maximum_confidence=float(payload.get("maximum_confidence", 1.0)),
                    expected_source_type=payload.get("expected_source_type"),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ParserFixtureError(f"Fixture {path.name} is invalid: {error}") from error
    return tuple(cases)


def _ordered(observations: Sequence[ParsedObservation]) -> tuple[ParsedObservation, ...]:
    """Order observations the way the screen reads: top to bottom."""

    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.source_region.top if item.source_region is not None else 0,
                item.source_region.left if item.source_region is not None else 0,
            ),
        )
    )


def _compare(
    case: ParserFixtureCase,
    selection: ParserSelection,
) -> ParserMetrics:
    observed_items = selection.observations
    if selection.statement is not None:
        observed_items = (selection.statement.summary, *observed_items)
    observed = _ordered(observed_items)
    compared = min(len(case.expected), len(observed))
    mismatches: list[str] = []
    amount_matches = date_matches = merchant_matches = 0
    direction_matches = metadata_matches = 0

    for index in range(compared):
        expected = case.expected[index]
        actual = observed[index]
        if actual.amount_minor == expected.amount_minor and (
            expected.currency is None or actual.currency == expected.currency
        ):
            amount_matches += 1
        else:
            mismatches.append(
                f"row {index} amount {actual.amount_minor} {actual.currency} != "
                f"{expected.amount_minor} {expected.currency}"
            )
        if actual.occurred_on == expected.occurred_on:
            date_matches += 1
        else:
            mismatches.append(f"row {index} date {actual.occurred_on} != {expected.occurred_on}")
        if actual.merchant == expected.merchant:
            merchant_matches += 1
        else:
            mismatches.append(f"row {index} merchant {actual.merchant!r} != {expected.merchant!r}")
        if expected.direction is None or actual.direction == expected.direction:
            direction_matches += 1
        else:
            mismatches.append(f"row {index} direction {actual.direction} != {expected.direction}")

        metadata_problems = [
            name
            for name, actual_value, expected_value in (
                ("instrument_suffix", actual.instrument_suffix, expected.instrument_suffix),
                ("counterparty", actual.counterparty, expected.counterparty),
                ("approval_code", actual.approval_code, expected.approval_code),
                ("installment_months", actual.installment_months, expected.installment_months),
                ("is_settlement", actual.is_settlement, expected.is_settlement),
                (
                    "balance_after",
                    None if actual.balance_after is None else int(actual.balance_after),
                    expected.balance_after_minor,
                ),
            )
            if expected_value is not None and actual_value != expected_value
        ]
        if metadata_problems:
            mismatches.append(f"row {index} metadata mismatch: {', '.join(metadata_problems)}")
        else:
            metadata_matches += 1

    return ParserMetrics(
        name=case.name,
        parser=case.parser,
        institution=case.institution,
        selected_parser=selection.metadata.name,
        support_score=selection.support.score,
        detected_source_type=selection.support.detected_source_type,
        expected_count=len(case.expected),
        observed_count=len(observed),
        compared=compared,
        amount_matches=amount_matches,
        date_matches=date_matches,
        merchant_matches=merchant_matches,
        direction_matches=direction_matches,
        metadata_matches=metadata_matches,
        missed=max(0, len(case.expected) - len(observed)),
        false_positives=max(0, len(observed) - len(case.expected)),
        mismatches=tuple(mismatches),
    )


def run_parser_fixture_suite(
    cases: Sequence[ParserFixtureCase], *, registry: ParserRegistry
) -> tuple[ParserMetrics, ...]:
    """Parse every case through the registry and score the results."""

    return tuple(_compare(case, registry.parse(case.document, case.tokens)) for case in cases)


def _aggregate(metrics: Sequence[ParserMetrics]) -> AccuracyAggregate:
    return AccuracyAggregate(
        fixtures=len(metrics),
        expected_count=sum(item.expected_count for item in metrics),
        observed_count=sum(item.observed_count for item in metrics),
        compared=sum(item.compared for item in metrics),
        amount_matches=sum(item.amount_matches for item in metrics),
        date_matches=sum(item.date_matches for item in metrics),
        merchant_matches=sum(item.merchant_matches for item in metrics),
        missed=sum(item.missed for item in metrics),
        false_positives=sum(item.false_positives for item in metrics),
    )


def build_accuracy_report(metrics: Sequence[ParserMetrics]) -> ParserAccuracyReport:
    """Aggregate fixture metrics per parser, per institution, and overall."""
    parser_groups: dict[str, list[ParserMetrics]] = defaultdict(list)
    institution_groups: dict[str, list[ParserMetrics]] = defaultdict(list)
    for item in metrics:
        parser_groups[item.parser].append(item)
        institution_groups[item.institution].append(item)
    return ParserAccuracyReport(
        overall=_aggregate(metrics),
        by_parser={name: _aggregate(group) for name, group in sorted(parser_groups.items())},
        by_institution={
            name: _aggregate(group) for name, group in sorted(institution_groups.items())
        },
    )


def summarize_report(report: ParserAccuracyReport) -> str:
    """Render concise human output alongside the JSON report."""
    lines: list[str] = []
    for level, groups in (("parser", report.by_parser), ("institution", report.by_institution)):
        for name, item in sorted(groups.items()):
            lines.append(
                f"{level}={name} fixtures={item.fixtures} amount={item.amount_accuracy:.4f} "
                f"date={item.date_accuracy:.4f} merchant={item.merchant_accuracy:.4f} "
                f"missed={item.missed_rate:.4f} false={item.false_rate:.4f}"
            )
    item = report.overall
    lines.append(
        f"overall fixtures={item.fixtures} amount={item.amount_accuracy:.4f} "
        f"date={item.date_accuracy:.4f} merchant={item.merchant_accuracy:.4f} "
        f"missed={item.missed_rate:.4f} false={item.false_rate:.4f}"
    )
    return "\n".join(lines)


def summarize(metrics: Sequence[ParserMetrics]) -> str:
    """A short human-readable regression report, useful in test failures."""

    lines = [
        f"{item.name}: amount={item.amount_accuracy:.2f} date={item.date_accuracy:.2f} "
        f"merchant={item.merchant_accuracy:.2f} missed={item.missed_rate:.2f} "
        f"false={item.false_rate:.2f} parser={item.selected_parser}"
        for item in metrics
    ]
    return "\n".join(lines)
