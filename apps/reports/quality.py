"""Build daily parser and OCR quality trends without copying financial data."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core import metrics
from apps.observations.models import ImportedObservation
from apps.reconciliation.models import ReconciliationMatch

from .models import QualityMetricDaily

LOW_CONFIDENCE = 0.8
UNKNOWN = "unknown"


@dataclass
class _Counts:
    observations: int = 0
    corrected: int = 0
    disagreements: int = 0
    duplicate_candidates: int = 0
    duplicate_confirmed: int = 0
    ocr_issues: int = 0
    parser_issues: int = 0
    confidence_total: Decimal = Decimal("0")


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.00001"))


def _dimension(observation: ImportedObservation) -> tuple[str, str, str]:
    institution = observation.parser_name or UNKNOWN
    source_type = observation.source_document.effective_source_type or UNKNOWN
    engine = observation.ocr_run.engine if observation.ocr_run is not None else UNKNOWN
    return institution[:64], source_type[:40], engine[:32]


def _parser_has_issue(observation: ImportedObservation) -> bool:
    flags = {str(flag) for flag in observation.review_flags or ()}
    return (
        observation.parser_confidence < LOW_CONFIDENCE
        or observation.has_missing_fields
        or observation.balance_mismatched
        or bool(flags & {"parser_error", "parser_fallback"})
    )


def _metric_defaults(counts: _Counts) -> dict[str, Any]:
    return {
        "observations_count": counts.observations,
        "corrected_count": counts.corrected,
        "disagreement_count": counts.disagreements,
        "duplicate_candidates_count": counts.duplicate_candidates,
        "duplicate_confirmed_count": counts.duplicate_confirmed,
        "ocr_issue_count": counts.ocr_issues,
        "parser_issue_count": counts.parser_issues,
        "correction_rate": _rate(counts.corrected, counts.observations),
        "disagreement_rate": _rate(counts.disagreements, counts.observations),
        "duplicate_rate": _rate(counts.duplicate_confirmed, counts.observations),
        "ocr_issue_rate": _rate(counts.ocr_issues, counts.observations),
        "parser_issue_rate": _rate(counts.parser_issues, counts.observations),
        "mean_confidence": (
            counts.confidence_total / counts.observations if counts.observations else Decimal("0")
        ).quantize(Decimal("0.00001")),
    }


@transaction.atomic
def aggregate_day(day: date) -> tuple[QualityMetricDaily, ...]:
    """Rebuild one day's dimensions, making reruns exact and idempotent."""

    grouped: dict[tuple[str, str, str], _Counts] = defaultdict(_Counts)
    observations = ImportedObservation.objects.select_related("source_document", "ocr_run").filter(
        created_at__date=day
    )
    observation_dimensions: dict[Any, tuple[str, str, str]] = {}
    for observation in observations.iterator():
        dimension = _dimension(observation)
        observation_dimensions[observation.pk] = dimension
        counts = grouped[dimension]
        counts.observations += 1
        counts.confidence_total += Decimal(str(observation.ocr_confidence))
        counts.corrected += int(
            observation.review_status == ImportedObservation.ReviewStatus.CORRECTED
            or bool(observation.corrected_fields)
        )
        counts.disagreements += int(observation.balance_mismatched)
        counts.ocr_issues += int(observation.ocr_confidence < LOW_CONFIDENCE)
        counts.parser_issues += int(_parser_has_issue(observation))

    matches = ReconciliationMatch.objects.filter(
        created_at__date=day,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
    ).values_list("left_observation_id", "status")
    for observation_id, status in matches:
        match_dimension: tuple[str, str, str] | None = observation_dimensions.get(observation_id)
        if match_dimension is None:
            match_observation = (
                ImportedObservation.objects.select_related("source_document", "ocr_run")
                .filter(pk=observation_id)
                .first()
            )
            if match_observation is None:
                continue
            match_dimension = _dimension(match_observation)
        counts = grouped[match_dimension]
        counts.duplicate_candidates += 1
        counts.duplicate_confirmed += int(status == ReconciliationMatch.Status.CONFIRMED)

    QualityMetricDaily.objects.filter(day=day).delete()
    rows = tuple(
        QualityMetricDaily(
            day=day,
            institution=institution,
            source_type=source_type,
            engine=engine,
            **_metric_defaults(counts),
        )
        for (institution, source_type, engine), counts in sorted(grouped.items())
    )
    if rows:
        QualityMetricDaily.objects.bulk_create(rows)
    metrics.record(metrics.QUALITY_DAILY_AGGREGATE, value=len(rows), status="completed")
    return rows


def iter_days(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def aggregate_range(start: date, end: date) -> int:
    if end < start:
        raise ValueError("The quality metric end date cannot precede its start date.")
    return sum(len(aggregate_day(day)) for day in iter_days(start, end))


def default_day() -> date:
    return timezone.localdate()
