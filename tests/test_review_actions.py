import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from apps.core.crypto import decrypt_model_field
from apps.core.errors import ConflictError, ForbiddenError
from apps.core.models import AuditEvent
from apps.ledger.models import LedgerEntry
from apps.observations.models import ImportedObservation
from apps.observations.review import (
    ObservationActionError,
    accept_observation,
    correct_observation,
    decrypt_observation,
    merge_observations,
    reject_observation,
)
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.transactions.models import CanonicalTransaction
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_user,
)

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)


@pytest.fixture
def owner() -> Any:
    return make_user(email="review-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("4200"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "스타벅스",
        "confidence_factors": {
            "token_confidence": 0.95,
            "date_confidence": 1.0,
            "amount_confidence": 0.98,
            "direction_confidence": 0.95,
        },
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, *observations: ParsedObservation, sha: str = "3" * 64) -> Any:
    document = make_document(user, file_sha256=sha)
    run = make_ocr_run(user, document)
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            observations,
        ),
        data_key=KEY,
        key_version=1,
    ).observations


# ---------------------------------------------------------------------------
# Corrections (#65)
# ---------------------------------------------------------------------------


def test_every_documented_field_can_be_corrected(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    account = make_account(owner)

    corrected = correct_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        key_version=1,
        corrections={
            "merchant": "이디야커피",
            "amount_minor": 5300,
            "occurred_at": date(2026, 8, 14),
            "direction": ImportedObservation.Direction.CREDIT,
            "transaction_type_guess": CanonicalTransaction.TransactionType.REFUND,
            "financial_account_guess": account,
        },
    )

    assert decrypt_model_field(corrected, "merchant_raw_encrypted", key=KEY) == "이디야커피"
    assert decrypt_model_field(corrected, "amount_encrypted", key=KEY) == "5300:KRW"
    assert corrected.occurred_at == date(2026, 8, 14)
    assert corrected.direction == ImportedObservation.Direction.CREDIT
    assert corrected.transaction_type_guess == CanonicalTransaction.TransactionType.REFUND
    assert corrected.financial_account_guess_id == account.pk
    assert corrected.review_status == ImportedObservation.ReviewStatus.CORRECTED


def test_corrections_record_which_fields_changed(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    corrected = correct_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        key_version=1,
        corrections={"merchant": "이디야커피", "amount_minor": 4200},
    )

    # The amount was already 4200, so only the merchant counts as corrected.
    assert corrected.corrected_fields == ["merchant"]


@pytest.mark.parametrize(
    "corrections",
    [
        {"amount_minor": 0},
        {"amount_minor": -100},
        {"amount_minor": "not a number"},
        {"currency": "WONS"},
        {"currency": "K9W"},
        {"direction": "sideways"},
        {"transaction_type_guess": "teleport"},
        {"installment_months": 0},
        {"occurred_at": "2026-08-15"},
    ],
)
def test_invalid_corrections_cannot_be_saved(owner: Any, corrections: dict[str, Any]) -> None:
    row = seed(owner, parsed())[0]

    with pytest.raises(ObservationActionError):
        correct_observation(
            row.pk, user=owner, data_key=KEY, key_version=1, corrections=corrections
        )

    row.refresh_from_db()
    assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert row.corrected_fields == []


def test_correcting_the_currency_re_encodes_the_stored_amount(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    corrected = correct_observation(
        row.pk, user=owner, data_key=KEY, key_version=1, corrections={"currency": "USD"}
    )

    # A stale currency inside the amount would post in the wrong currency.
    assert corrected.currency == "USD"
    assert decrypt_model_field(corrected, "amount_encrypted", key=KEY) == "4200:USD"


def test_a_currency_and_amount_corrected_together_agree(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    corrected = correct_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        key_version=1,
        corrections={"amount_minor": 1025, "currency": "USD"},
    )

    assert decrypt_model_field(corrected, "amount_encrypted", key=KEY) == "1025:USD"


def test_unknown_correction_fields_are_refused_not_ignored(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    with pytest.raises(ObservationActionError, match="Unknown correctable fields"):
        correct_observation(
            row.pk, user=owner, data_key=KEY, key_version=1, corrections={"amuont": 1}
        )


def test_corrections_generate_an_audit_event_without_values(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    correct_observation(
        row.pk, user=owner, data_key=KEY, key_version=1, corrections={"merchant": "이디야커피"}
    )

    event = AuditEvent.objects.filter(
        user=owner, event_type=AuditEvent.EventType.OBSERVATION_CORRECTED
    ).get()
    assert event.metadata["fields"] == ["merchant"]
    assert "이디야커피" not in str(event.metadata)


def test_correcting_a_flagged_field_clears_its_flag(owner: Any) -> None:
    row = seed(owner, parsed(merchant=None, missing_fields=frozenset({"merchant"})))[0]
    assert "missing_merchant" in row.review_flags

    corrected = correct_observation(
        row.pk, user=owner, data_key=KEY, key_version=1, corrections={"merchant": "이디야커피"}
    )

    assert "missing_merchant" not in corrected.review_flags
    assert corrected.has_missing_fields is False


def test_another_users_observation_cannot_be_corrected(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    intruder = make_user(email="intruder-review@example.com")

    with pytest.raises(ForbiddenError):
        correct_observation(
            row.pk, user=intruder, data_key=KEY, key_version=1, corrections={"merchant": "x"}
        )


# ---------------------------------------------------------------------------
# Accept, reject, merge (#66) and canonical creation (#68)
# ---------------------------------------------------------------------------


def test_accepting_creates_exactly_one_canonical_transaction(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    account = make_account(owner)

    canonical = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    row.refresh_from_db()
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1
    assert row.canonical_transaction_id == canonical.pk
    assert row.review_status == ImportedObservation.ReviewStatus.ACCEPTED
    assert row.feeds_reports is True
    assert canonical.amount_encrypted == "4200:KRW"
    assert canonical.reviewed_by_id == owner.pk


def test_repeated_acceptance_is_idempotent(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    account = make_account(owner)

    first = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    second = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    assert first.pk == second.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_a_corrected_row_keeps_its_corrected_status_on_acceptance(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    account = make_account(owner)
    correct_observation(
        row.pk, user=owner, data_key=KEY, key_version=1, corrections={"merchant": "이디야커피"}
    )

    accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    row.refresh_from_db()
    assert row.review_status == ImportedObservation.ReviewStatus.CORRECTED
    assert row.feeds_reports is True


def test_a_risky_row_needs_explicit_confirmation(owner: Any) -> None:
    row = seed(owner, parsed(ambiguous_fields=frozenset({"amount"})))[0]
    account = make_account(owner)

    with pytest.raises(ConflictError, match="confirm explicitly"):
        accept_observation(
            row.pk,
            user=owner,
            data_key=KEY,
            financial_account=account,
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        )

    canonical = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        confirmed=True,
    )
    assert canonical.pk is not None


def test_rows_without_an_amount_or_date_cannot_be_accepted(owner: Any) -> None:
    account = make_account(owner)
    no_amount = seed(owner, parsed(amount=None, currency=None))[0]
    no_date = seed(owner, parsed(occurred_on=None), sha="4" * 64)[0]

    for row in (no_amount, no_date):
        with pytest.raises(ObservationActionError):
            accept_observation(
                row.pk,
                user=owner,
                data_key=KEY,
                financial_account=account,
                transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
                confirmed=True,
            )

    assert CanonicalTransaction.objects.filter(user=owner).count() == 0


def test_rejected_rows_never_feed_reports(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    rejected = reject_observation(row.pk, user=owner, reason="not mine")

    assert rejected.review_status == ImportedObservation.ReviewStatus.REJECTED
    assert rejected.feeds_reports is False
    assert rejected.canonical_transaction_id is None
    assert CanonicalTransaction.objects.filter(user=owner).count() == 0
    event = AuditEvent.objects.filter(
        user=owner, event_type=AuditEvent.EventType.OBSERVATION_REJECTED
    ).get()
    assert "not mine" not in str(event.metadata)


def test_an_accepted_row_cannot_be_rejected(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=make_account(owner),
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    with pytest.raises(ConflictError):
        reject_observation(row.pk, user=owner)


def test_rejecting_twice_is_idempotent(owner: Any) -> None:
    row = seed(owner, parsed())[0]

    reject_observation(row.pk, user=owner)
    again = reject_observation(row.pk, user=owner)

    assert again.review_status == ImportedObservation.ReviewStatus.REJECTED


def test_merging_keeps_every_source_traceable(owner: Any) -> None:
    rows = seed(owner, parsed(), parsed(merchant="같은거래"))

    winner = merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[1].pk])

    rows[1].refresh_from_db()
    assert winner.pk == rows[0].pk
    assert rows[1].review_status == ImportedObservation.ReviewStatus.MERGED
    assert rows[1].merged_into_id == rows[0].pk
    # The merged row is preserved, not deleted, with its own document intact.
    assert ImportedObservation.objects.filter(pk=rows[1].pk).exists()
    assert rows[1].source_document_id is not None
    assert rows[1].feeds_reports is False


def test_merging_is_idempotent_and_refuses_self_merge(owner: Any) -> None:
    rows = seed(owner, parsed(), parsed(merchant="같은거래"))

    merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[1].pk])
    merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[1].pk])

    assert ImportedObservation.objects.filter(merged_into=rows[0]).count() == 1
    with pytest.raises(ObservationActionError):
        merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[0].pk])


def test_merging_cannot_discard_a_confirmed_transaction(owner: Any) -> None:
    rows = seed(owner, parsed(), parsed(merchant="같은거래"))
    accept_observation(
        rows[1].pk,
        user=owner,
        data_key=KEY,
        financial_account=make_account(owner),
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    with pytest.raises(ConflictError):
        merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[1].pk])

    rows[1].refresh_from_db()
    assert rows[1].canonical_transaction_id is not None


def test_actions_refuse_observations_owned_by_another_user(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    intruder = make_user(email="intruder-actions@example.com")

    with pytest.raises(ForbiddenError):
        accept_observation(row.pk, user=intruder, data_key=KEY)
    with pytest.raises(ForbiddenError):
        reject_observation(row.pk, user=intruder)
    with pytest.raises(ForbiddenError):
        merge_observations(user=intruder, winner_id=row.pk, duplicate_ids=[])
    with pytest.raises(ForbiddenError):
        decrypt_observation(row, user=intruder, data_key=KEY)


def test_ledger_posting_and_status_change_are_atomic(owner: Any) -> None:
    row = seed(owner, parsed())[0]
    account = make_account(owner)
    ledger_accounts = make_ledger_accounts(owner, account)

    canonical = accept_observation(
        row.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        ledger_accounts=ledger_accounts,
    )

    row.refresh_from_db()
    assert canonical.status == CanonicalTransaction.Status.CONFIRMED
    assert LedgerEntry.objects.filter(transaction=canonical).count() == 2
    assert row.review_status == ImportedObservation.ReviewStatus.ACCEPTED
    assert row.canonical_transaction_id == canonical.pk


def test_a_failed_ledger_posting_rolls_back_the_acceptance(owner: Any) -> None:
    from apps.ledger.rules import PostingRuleAccounts

    row = seed(owner, parsed())[0]
    account = make_account(owner)
    ledger_accounts = make_ledger_accounts(owner, account)
    # A transfer needs a destination account; without one the posting fails.
    incomplete = PostingRuleAccounts(account=ledger_accounts.account)

    with pytest.raises(Exception):  # noqa: B017 - any posting failure must roll back
        accept_observation(
            row.pk,
            user=owner,
            data_key=KEY,
            financial_account=account,
            transaction_type=CanonicalTransaction.TransactionType.INTERNAL_TRANSFER,
            ledger_accounts=incomplete,
        )

    row.refresh_from_db()
    assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
    assert row.canonical_transaction_id is None
    assert CanonicalTransaction.objects.filter(user=owner).count() == 0
    assert LedgerEntry.objects.count() == 0
