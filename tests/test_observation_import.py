import os
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import transaction as db_transaction

from apps.core.crypto import decrypt_model_field
from apps.core.errors import InvalidRequestError
from apps.core.models import AuditEvent
from apps.observations.models import ImportedObservation
from apps.observations.services import (
    ObservationImportError,
    import_parser_selection,
    observations_for_document,
)
from apps.ocr.contracts import BoundingBox
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from tests.factories import make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner() -> Any:
    return make_user(email="observation-owner@example.com")


@pytest.fixture
def data_key() -> bytes:
    return os.urandom(32)


def parsed(
    *,
    merchant: str | None = "스타벅스",
    amount: Decimal | None = Decimal("4200"),
    currency: str | None = "KRW",
    direction: TransactionDirection | None = TransactionDirection.DEBIT,
    occurred_on: date | None = date(2026, 8, 15),
    **overrides: Any,
) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": occurred_on,
        "amount": amount,
        "currency": currency,
        "direction": direction,
        "merchant": merchant,
        "source_region": BoundingBox(0, 10, 300, 40),
        "confidence_factors": {
            "token_confidence": 0.94,
            "date_confidence": 1.0,
            "amount_confidence": 0.98,
            "direction_confidence": 0.95,
            "balance_status": "valid",
        },
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def selection(*observations: ParsedObservation, name: str = "toss_bank") -> ParserSelection:
    return ParserSelection(
        ParserMetadata(name, "1.0"),
        ParserSupport(0.95, "bank_transaction_list", ("matched",)),
        tuple(observations),
    )


def test_import_persists_rows_in_screen_order(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    result = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed(merchant="첫번째"), parsed(merchant="두번째")),
        data_key=data_key,
        key_version=1,
    )

    assert result.created is True
    assert result.count == 2
    assert [item.row_index for item in result.observations] == [0, 1]
    stored = list(observations_for_document(document))
    assert [item.row_index for item in stored] == [0, 1]
    assert all(item.user_id == owner.pk for item in stored)
    assert all(item.source_document_id == document.pk for item in stored)


def test_values_are_encrypted_and_round_trip(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    observation = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed(approval_code="12345678")),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    assert "스타벅스" not in observation.merchant_raw_encrypted
    assert decrypt_model_field(observation, "merchant_raw_encrypted", key=data_key) == "스타벅스"
    assert decrypt_model_field(observation, "amount_encrypted", key=data_key) == "4200:KRW"
    assert decrypt_model_field(observation, "approval_code_encrypted", key=data_key) == "12345678"
    assert (
        decrypt_model_field(observation, "source_region_json_encrypted", key=data_key)
        == '{"bottom":40,"left":0,"right":300,"top":10}'
    )


def test_confidences_are_stored_independently(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    observation = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    assert observation.ocr_confidence == pytest.approx(0.94)
    assert observation.parser_confidence == pytest.approx((1.0 + 0.98 + 0.95) / 3)
    # A sharp scan parsed poorly, or the reverse, must not look confident.
    assert observation.overall_confidence == pytest.approx(
        min(observation.ocr_confidence, observation.parser_confidence)
    )


def test_review_flags_name_concerns_without_leaking_values(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    observation = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(
            parsed(
                merchant=None,
                direction=TransactionDirection.UNKNOWN,
                missing_fields=frozenset({"merchant", "direction"}),
                ambiguous_fields=frozenset({"amount"}),
                confidence_factors={"token_confidence": 0.5, "balance_status": "invalid"},
            )
        ),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    assert observation.review_flags == [
        "ambiguous_amount",
        "balance_mismatch",
        "missing_direction",
        "missing_merchant",
        "unknown_direction",
    ]
    assert observation.requires_review is True
    assert observation.direction == ImportedObservation.Direction.UNKNOWN
    # Flags are field names only, never the values behind them.
    assert all("스타벅스" not in flag for flag in observation.review_flags)


def test_reimporting_the_same_run_is_idempotent(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)
    payload = selection(parsed(), parsed(merchant="두번째"))

    first = import_parser_selection(
        document=document, ocr_run=run, selection=payload, data_key=data_key, key_version=1
    )
    second = import_parser_selection(
        document=document, ocr_run=run, selection=payload, data_key=data_key, key_version=1
    )

    assert first.created is True
    assert second.created is False
    assert second.count == 2
    assert ImportedObservation.objects.filter(source_document=document).count() == 2


def test_the_database_rejects_a_duplicate_import_key(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)
    observation = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    duplicate = ImportedObservation.objects.get(pk=observation.pk)
    duplicate.pk = uuid.uuid4()
    with pytest.raises(IntegrityError), db_transaction.atomic():
        duplicate.save(force_insert=True)


def test_import_keys_are_stable_for_the_same_origin(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    assert len(row.import_key) == 64
    assert row.import_key == ImportedObservation.build_import_key(
        source_document_id=document.pk,
        ocr_run_id=run.pk,
        parser_name="toss_bank",
        parser_version="1.0",
        parser_output_version=1,
        row_index=0,
    )


def test_a_new_parser_version_imports_alongside_the_old_one(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    )
    upgraded = ParserSelection(
        ParserMetadata("toss_bank", "2.0"),
        ParserSupport(0.95, "bank_transaction_list", ()),
        (parsed(parser_version="2.0"),),
    )
    second = import_parser_selection(
        document=document, ocr_run=run, selection=upgraded, data_key=data_key, key_version=1
    )

    assert second.created is True
    assert ImportedObservation.objects.filter(source_document=document).count() == 2


def test_a_failed_row_rolls_back_the_whole_import(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    # The second row carries an unusable currency, so its validation fails.
    with pytest.raises(ValidationError):
        import_parser_selection(
            document=document,
            ocr_run=run,
            selection=selection(parsed(), parsed(currency="NOT-A-CODE")),
            data_key=data_key,
            key_version=1,
        )

    # The first row must not survive on its own: review never sees half a page.
    assert ImportedObservation.objects.filter(source_document=document).count() == 0


def test_an_ocr_run_from_another_user_is_refused(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    intruder = make_user(email="intruder@example.com")
    foreign_document = make_document(intruder, file_sha256="8" * 64)
    foreign_run = make_ocr_run(intruder, foreign_document)

    with pytest.raises(ObservationImportError):
        import_parser_selection(
            document=document,
            ocr_run=foreign_run,
            selection=selection(parsed()),
            data_key=data_key,
            key_version=1,
        )

    assert ImportedObservation.objects.filter(source_document=document).count() == 0
    assert issubclass(ObservationImportError, InvalidRequestError)


def test_import_records_an_audit_event(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    )

    event = AuditEvent.objects.filter(
        user=owner, event_type=AuditEvent.EventType.OBSERVATIONS_IMPORTED
    ).get()
    assert event.metadata["parser"] == "toss_bank"
    assert event.metadata["observation_count"] == 1
    # Audit metadata carries identifiers and counts, never financial values.
    assert "4200" not in str(event.metadata)


def test_observations_never_feed_reports_before_acceptance(owner: Any, data_key: bytes) -> None:
    document = make_document(owner)
    run = make_ocr_run(owner, document)

    observation = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=selection(parsed()),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    assert observation.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert observation.is_open is True
    assert observation.feeds_reports is False
