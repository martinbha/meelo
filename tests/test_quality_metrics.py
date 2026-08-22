from __future__ import annotations

import json
from datetime import date, datetime
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.observations.models import ImportedObservation
from apps.processing.models import SourceDocument
from apps.reconciliation.models import ReconciliationMatch
from apps.reports.models import QualityMetricDaily
from apps.reports.quality import aggregate_day
from tests.factories import make_user

pytestmark = pytest.mark.django_db


def _document(user: Any) -> SourceDocument:
    return SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="fixture.png",
        mime_type="image/png",
        file_size=12,
        source_type=SourceDocument.SourceType.BANK_TRANSACTION_LIST,
    )


def _observation(document: SourceDocument, **values: Any) -> ImportedObservation:
    defaults: dict[str, Any] = {
        "user": document.user,
        "source_document": document,
        "parser_name": "toss_bank",
        "parser_version": "1",
        "ocr_confidence": 0.95,
        "parser_confidence": 0.95,
        "overall_confidence": 0.95,
    }
    defaults.update(values)
    return ImportedObservation.objects.create(**defaults)


def _set_day(instance: Any, day: date) -> None:
    timestamp = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    instance.__class__.objects.filter(pk=instance.pk).update(created_at=timestamp)


def test_quality_aggregation_is_dimensioned_and_idempotent() -> None:
    user = make_user(email="quality@example.com")
    document = _document(user)
    first = _observation(
        document,
        review_status=ImportedObservation.ReviewStatus.CORRECTED,
        corrected_fields=["merchant"],
        balance_mismatched=True,
        ocr_confidence=0.4,
    )
    second = _observation(document, has_missing_fields=True, parser_confidence=0.5)
    day = date(2026, 8, 22)
    _set_day(first, day)
    _set_day(second, day)
    match = ReconciliationMatch.objects.create(
        user=user,
        left_observation=first,
        right_observation=second,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        status=ReconciliationMatch.Status.CONFIRMED,
    )
    _set_day(match, day)

    rows = aggregate_day(day)
    assert len(rows) == 1
    row = rows[0]
    assert row.observations_count == 2
    assert row.corrected_count == 1
    assert row.disagreement_count == 1
    assert row.duplicate_candidates_count == 1
    assert row.duplicate_confirmed_count == 1
    assert row.ocr_issue_count == 1
    assert row.parser_issue_count == 2
    assert str(row.correction_rate) == "0.50000"
    assert str(row.duplicate_rate) == "0.50000"

    aggregate_day(day)
    assert QualityMetricDaily.objects.filter(day=day).count() == 1


def test_quality_command_can_backfill_and_emit_machine_output() -> None:
    user = make_user(email="quality-command@example.com")
    document = _document(user)
    observation = _observation(document)
    day = date(2026, 8, 21)
    _set_day(observation, day)
    output = StringIO()

    call_command("quality_metrics", "--date", day.isoformat(), "--json", stdout=output)

    payload = json.loads(output.getvalue())
    assert payload[0]["day"] == day.isoformat()
    assert payload[0]["institution"] == "toss_bank"
    assert payload[0]["source_type"] == "bank_transaction_list"
    assert "merchant" not in output.getvalue()
