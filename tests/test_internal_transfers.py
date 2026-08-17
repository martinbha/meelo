"""Internal transfers between a user's own accounts (#76, specification 7.4, 17.3).

The mistake this file guards against is counting one move twice. Money leaving
checking and arriving in savings is two screenshot rows and one event; read
separately they look like a purchase and a payday.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apps.core.errors import ConflictError, ForbiddenError
from apps.core.models import AuditEvent
from apps.ledger.models import LedgerEntry
from apps.observations.models import ImportedObservation
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reconciliation.duplicates import ObservationFacts
from apps.reconciliation.matching import (
    STRONG_MATCH_SCORE,
    TransferTolerance,
    match_internal_transfer,
)
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.services import (
    ReconciliationError,
    confirm_match,
    record_match,
    reject_match,
)
from apps.reconciliation.transfers import confirm_internal_transfer
from apps.transactions.classification import is_income, is_neutral, is_spending
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

DEBIT = ImportedObservation.Direction.DEBIT
CREDIT = ImportedObservation.Direction.CREDIT


def facts(**overrides: Any) -> ObservationFacts:
    values: dict[str, Any] = {
        "observation_id": "row-1",
        "user_id": 1,
        "occurred_at": date(2026, 8, 15),
        "amount_minor": 500_000,
        "currency": "KRW",
        "direction": DEBIT,
        "merchant": "",
        "approval_code": "",
        "balance_after_minor": None,
        "instrument_id": None,
        "account_id": None,
        "source_type": "",
        "source_document_id": "doc-1",
    }
    values.update(overrides)
    return ObservationFacts(**values)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_a_slightly_different_amount_is_found_only_when_the_reviewer_asks() -> None:
    # A wire fee shaved 500 off the arriving side.
    outgoing = facts(observation_id="out", account_id="account-1")
    incoming = facts(
        observation_id="in", account_id="account-2", direction=CREDIT, amount_minor=499_500
    )

    assert match_internal_transfer(outgoing, incoming) is None

    proposal = match_internal_transfer(
        outgoing, incoming, tolerance=TransferTolerance(amount_minor=1_000)
    )

    assert proposal is not None
    assert proposal.match_type == "internal_transfer"
    assert "approximate_amount" in proposal.features
    assert "exact_amount" not in proposal.features


def test_a_date_beyond_the_strict_window_is_found_only_when_the_reviewer_asks() -> None:
    outgoing = facts(observation_id="out", account_id="account-1")
    incoming = facts(
        observation_id="in",
        account_id="account-2",
        direction=CREDIT,
        occurred_at=date(2026, 8, 18),
    )

    assert match_internal_transfer(outgoing, incoming) is None

    proposal = match_internal_transfer(outgoing, incoming, tolerance=TransferTolerance())

    assert proposal is not None
    assert "exact_amount" in proposal.features
    assert "reviewer_tolerance" in proposal.features


def test_a_tolerated_pair_always_needs_confirmation() -> None:
    outgoing = facts(observation_id="out", account_id="account-1")
    incoming = facts(
        observation_id="in", account_id="account-2", direction=CREDIT, amount_minor=499_500
    )

    proposal = match_internal_transfer(
        outgoing, incoming, tolerance=TransferTolerance(amount_minor=1_000)
    )

    assert proposal is not None
    # A pair found because the reviewer widened the search is evidence of their
    # intent, not of the transfer, so it can never link on its own.
    assert proposal.score < STRONG_MATCH_SCORE
    assert proposal.needs_review


def test_tolerance_does_not_reach_past_its_own_limits() -> None:
    outgoing = facts(observation_id="out", account_id="account-1")
    far_off_amount = facts(
        observation_id="in", account_id="account-2", direction=CREDIT, amount_minor=400_000
    )
    far_off_date = facts(
        observation_id="in",
        account_id="account-2",
        direction=CREDIT,
        occurred_at=date(2026, 9, 30),
    )
    tolerance = TransferTolerance(amount_minor=1_000, window=timedelta(days=5))

    assert match_internal_transfer(outgoing, far_off_amount, tolerance=tolerance) is None
    assert match_internal_transfer(outgoing, far_off_date, tolerance=tolerance) is None


def test_a_tolerance_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        TransferTolerance(amount_minor=-1)
    with pytest.raises(ValueError):
        TransferTolerance(window=timedelta(days=-1))


def test_payment_to_an_external_counterparty_is_never_an_internal_transfer() -> None:
    """The arriving side of a real transfer sits in an account the user owns.

    Money sent to someone else has no such counterpart, so even a credit of the
    same amount on the same day must not pair with it.
    """

    outgoing = facts(observation_id="out", account_id="account-1")
    external = facts(observation_id="in", account_id=None, direction=CREDIT)
    generous = TransferTolerance(amount_minor=100_000, window=timedelta(days=30))

    assert match_internal_transfer(outgoing, external) is None
    assert match_internal_transfer(outgoing, external, tolerance=generous) is None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_an_internal_transfer_is_neither_income_nor_spending() -> None:
    transfer = CanonicalTransaction.TransactionType.INTERNAL_TRANSFER

    assert not is_spending(transfer)
    assert not is_income(transfer)
    assert is_neutral(transfer)

    assert is_spending(CanonicalTransaction.TransactionType.PURCHASE)
    assert is_income(CanonicalTransaction.TransactionType.INCOME)


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("500000"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "정기이체",
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


@pytest.fixture
def owner() -> Any:
    return make_user(email="transfer-owner@example.com")


def seed_transfer(user: Any) -> tuple[Any, Any, Any, Any]:
    """Two rows of one move, each mapped to a different owned account."""

    checking = make_account(user, name_blind_index="transfer-checking")
    savings = make_account(user, name_blind_index="transfer-savings")
    document = make_document(user, file_sha256="7" * 64)
    run = make_ocr_run(user, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(), parsed(direction=TransactionDirection.CREDIT)),
        ),
        data_key=KEY,
        key_version=1,
    ).observations
    outgoing, incoming = rows
    outgoing.financial_account_guess = checking
    outgoing.save(update_fields=["financial_account_guess"])
    incoming.financial_account_guess = savings
    incoming.save(update_fields=["financial_account_guess"])
    return outgoing, incoming, checking, savings


def propose(user: Any, outgoing: Any, incoming: Any) -> ReconciliationMatch:
    return record_match(
        user=user,
        left_observation_id=outgoing.pk,
        right_observation_id=incoming.pk,
        match_type=ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        score=85,
        features=("exact_amount", "same_date"),
    )


def test_both_sides_link_to_one_canonical_transfer(owner: Any) -> None:
    outgoing, incoming, checking, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)

    transfer = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert transfer.transaction_type == CanonicalTransaction.TransactionType.INTERNAL_TRANSFER
    assert transfer.financial_account_id == checking.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1
    for row in (outgoing, incoming):
        row.refresh_from_db()
        assert row.canonical_transaction_id == transfer.pk
        assert row.review_status == ImportedObservation.ReviewStatus.ACCEPTED

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.CONFIRMED


def test_the_side_order_of_the_match_does_not_decide_the_direction(owner: Any) -> None:
    """A match stores its rows in a fixed order unrelated to direction."""

    outgoing, incoming, checking, savings = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)
    # Whichever way round the pair was stored, the money still left checking.
    assert {match.left_observation_id, match.right_observation_id} == {outgoing.pk, incoming.pk}

    transfer = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert transfer.financial_account_id == checking.pk
    assert transfer.financial_account_id != savings.pk


def test_confirming_a_transfer_twice_returns_the_same_event(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)

    first = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)
    second = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert first.pk == second.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_a_side_already_accepted_alone_blocks_the_transfer(owner: Any) -> None:
    outgoing, incoming, checking, _ = seed_transfer(owner)
    accept_observation(
        outgoing.pk,
        user=owner,
        data_key=KEY,
        financial_account=checking,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    match = propose(owner, outgoing, incoming)

    with pytest.raises(ConflictError):
        confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    # Nothing was invented on top of the transaction that already exists.
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_an_unmapped_side_cannot_become_a_transfer(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    incoming.financial_account_guess = None
    incoming.save(update_fields=["financial_account_guess"])
    match = propose(owner, outgoing, incoming)

    with pytest.raises(ReconciliationError):
        confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    assert not CanonicalTransaction.objects.filter(user=owner).exists()


def test_two_rows_in_the_same_direction_cannot_become_a_transfer(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    incoming.direction = DEBIT
    incoming.save(update_fields=["direction"])
    match = propose(owner, outgoing, incoming)

    with pytest.raises(ReconciliationError):
        confirm_internal_transfer(match.pk, user=owner, data_key=KEY)


def test_a_rejected_candidate_cannot_become_a_transfer(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)
    reject_match(match.pk, user=owner)

    with pytest.raises(ConflictError):
        confirm_internal_transfer(match.pk, user=owner, data_key=KEY)


def test_the_plain_confirm_path_refuses_internal_transfers(owner: Any) -> None:
    """Confirming without creating the event would leave both sides acceptable."""

    outgoing, incoming, _, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)

    with pytest.raises(ReconciliationError):
        confirm_match(match.pk, user=owner)

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.PROPOSED


def test_another_users_transfer_cannot_be_confirmed(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)
    intruder = make_user(email="transfer-intruder@example.com")

    with pytest.raises(ForbiddenError):
        confirm_internal_transfer(match.pk, user=intruder, data_key=KEY)


def test_confirming_posts_the_ledger_in_the_same_transaction(owner: Any) -> None:
    outgoing, incoming, checking, savings = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)
    context = make_ledger_accounts(owner, checking, prefix="tr")
    destination = make_ledger_accounts(owner, savings, prefix="tr2").account
    posting_context = type(context)(account=context.account, transfer_account=destination)

    transfer = confirm_internal_transfer(
        match.pk, user=owner, data_key=KEY, ledger_accounts=posting_context
    )

    assert transfer.status == CanonicalTransaction.Status.CONFIRMED
    entries = LedgerEntry.objects.filter(transaction=transfer)
    assert entries.count() == 2
    assert {entry.amount_encrypted for entry in entries} == {"500000:KRW"}


def test_confirmation_is_audited_without_recording_any_value(owner: Any) -> None:
    outgoing, incoming, _, _ = seed_transfer(owner)
    match = propose(owner, outgoing, incoming)

    transfer = confirm_internal_transfer(match.pk, user=owner, data_key=KEY)

    event = AuditEvent.objects.filter(user=owner, event_type="internal_transfer_confirmed").first()
    assert event is not None
    assert event.metadata["canonical_transaction_id"] == str(transfer.pk)
    assert event.metadata["outgoing_observation_id"] == str(outgoing.pk)
    assert "500000" not in str(event.metadata)
