import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.core.errors import ConflictError, ForbiddenError
from apps.core.models import AuditEvent
from apps.observations.models import ImportedObservation
from apps.observations.reprocessing import (
    ReprocessError,
    is_latest_run,
    latest_run,
    request_reprocess,
)
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.processing.models import SourceDocument
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)


@pytest.fixture
def owner() -> Any:
    return make_user(email="reprocess-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("4200"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "스타벅스",
        "confidence_factors": {"token_confidence": 0.9, "amount_confidence": 0.9},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def import_for(user: Any, document: Any, run: Any) -> Any:
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(),),
        ),
        data_key=KEY,
        key_version=1,
    ).observations


def ready_document(user: Any, **overrides: Any) -> SourceDocument:
    document = make_document(
        user, processing_status=SourceDocument.Status.READY_FOR_REVIEW, **overrides
    )
    return document


def test_reprocessing_preserves_prior_runs_and_observations(owner: Any) -> None:
    document = ready_document(owner)
    run = make_ocr_run(owner, document)
    import_for(owner, document, run)

    result = request_reprocess(document.pk, user=owner)

    document.refresh_from_db()
    assert result.preserved_run_count == 1
    assert result.preserved_observation_count == 1
    assert document.processing_status == SourceDocument.Status.QUEUED
    assert ImportedObservation.objects.filter(source_document=document).count() == 1


def test_reprocessing_never_deletes_confirmed_transactions(owner: Any) -> None:
    document = ready_document(owner)
    run = make_ocr_run(owner, document)
    row = import_for(owner, document, run)[0]
    canonical = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=make_account(owner),
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    request_reprocess(document.pk, user=owner)

    row.refresh_from_db()
    assert CanonicalTransaction.objects.filter(pk=canonical.pk).exists()
    assert row.canonical_transaction_id == canonical.pk
    assert row.review_status == ImportedObservation.ReviewStatus.ACCEPTED


def test_the_latest_run_is_identifiable(owner: Any) -> None:
    document = ready_document(owner)
    first = make_ocr_run(owner, document)
    second = make_ocr_run(owner, document, engine="fallback")

    assert latest_run(document) is not None
    assert latest_run(document).pk == second.pk  # type: ignore[union-attr]
    assert is_latest_run(second) is True
    assert is_latest_run(first) is False


def test_concurrent_reruns_are_refused(owner: Any) -> None:
    document = ready_document(owner)

    request_reprocess(document.pk, user=owner)

    with pytest.raises(ConflictError, match="already being processed"):
        request_reprocess(document.pk, user=owner)


def test_a_failed_document_returns_to_a_reviewable_state(owner: Any) -> None:
    document = make_document(
        owner, processing_status=SourceDocument.Status.FAILED, error_code="OCR_TIMEOUT"
    )

    result = request_reprocess(document.pk, user=owner)

    document.refresh_from_db()
    assert result.previous_status == SourceDocument.Status.FAILED
    assert document.processing_status == SourceDocument.Status.QUEUED
    assert document.error_code == ""


def test_a_document_whose_image_was_deleted_cannot_be_reprocessed(owner: Any) -> None:
    document = ready_document(owner, original_deleted_at=timezone.now())

    with pytest.raises(ReprocessError, match="deleted"):
        request_reprocess(document.pk, user=owner)


def test_a_pending_document_cannot_be_reprocessed(owner: Any) -> None:
    document = make_document(owner, processing_status=SourceDocument.Status.PENDING)

    with pytest.raises(ReprocessError):
        request_reprocess(document.pk, user=owner)


def test_another_users_document_cannot_be_reprocessed(owner: Any) -> None:
    document = ready_document(owner)
    intruder = make_user(email="intruder-reprocess@example.com")

    with pytest.raises(ForbiddenError):
        request_reprocess(document.pk, user=intruder)

    document.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.READY_FOR_REVIEW


def test_reprocessing_is_audited(owner: Any) -> None:
    document = ready_document(owner)

    request_reprocess(document.pk, user=owner)

    event = AuditEvent.objects.filter(
        user=owner, event_type=AuditEvent.EventType.DOCUMENT_REPROCESS_REQUESTED
    ).get()
    assert event.metadata["previous_status"] == SourceDocument.Status.READY_FOR_REVIEW
    assert event.object_id == document.pk
