"""Refunds matched to the purchases they reverse (#77, specification 7.5, 17.4).

The mistake this file guards against is a returned coat looking like a payday.
A refund puts money back where it came from; treating the credit as income
inflates income and spending at once and leaves the category wrong.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from apps.categorization.models import Category
from apps.core.errors import ConflictError, ForbiddenError
from apps.core.models import AuditEvent
from apps.ledger.models import LedgerEntry
from apps.ledger.rules import PostingRuleAccounts
from apps.observations.models import ImportedObservation
from apps.observations.queue import QueueFilter, review_queue
from apps.observations.review import accept_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.refunds import (
    confirm_refund_match,
    propose_refund_matches,
    unmatched_refunds,
)
from apps.reconciliation.services import (
    ReconciliationError,
    confirm_match,
    queue_match_ids,
    reject_match,
)
from apps.transactions.classification import (
    is_income,
    is_spending,
    is_spending_reduction,
)
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

MERCHANT = "무신사"


@pytest.fixture
def owner() -> Any:
    return make_user(email="refund-owner@example.com")


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 10),
        "amount": Decimal("200000"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": MERCHANT,
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "hyundai_card",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def make_category(user: Any, name: str = "clothing") -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"refund-{name}",
        category_type=Category.CategoryType.EXPENSE,
    )


def seed_refund(user: Any) -> tuple[Any, Any, Any]:
    """A purchase and the refund that reverses it, both on one owned account."""

    account = make_account(user, name_blind_index="refund-card")
    document = make_document(user, file_sha256="6" * 64)
    run = make_ocr_run(user, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("hyundai_card", "1.0"),
            ParserSupport(0.95, "card_transaction_list", ()),
            (
                parsed(),
                parsed(direction=TransactionDirection.CREDIT, occurred_on=date(2026, 8, 20)),
            ),
        ),
        data_key=KEY,
        key_version=1,
    ).observations
    purchase, refund = rows
    for row in rows:
        row.financial_account_guess = account
        row.save(update_fields=["financial_account_guess"])
    return purchase, refund, account


# ---------------------------------------------------------------------------
# Candidate creation
# ---------------------------------------------------------------------------


def test_detection_pairs_a_refund_with_its_purchase(owner: Any) -> None:
    purchase, refund, _ = seed_refund(owner)

    stored = propose_refund_matches(user=owner, data_key=KEY)

    assert len(stored) == 1
    assert stored[0].match_type == ReconciliationMatch.MatchType.REFUND_MATCH
    assert {stored[0].left_observation_id, stored[0].right_observation_id} == {
        purchase.pk,
        refund.pk,
    }


def test_detection_still_finds_a_purchase_that_was_already_accepted(owner: Any) -> None:
    """Refunds usually arrive long after the purchase was reviewed."""

    purchase, _, account = seed_refund(owner)
    accept_observation(
        purchase.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )

    stored = propose_refund_matches(user=owner, data_key=KEY)

    assert len(stored) == 1


def test_detection_does_not_revive_a_rejected_candidate(owner: Any) -> None:
    seed_refund(owner)
    rejected = propose_refund_matches(user=owner, data_key=KEY)[0]
    reject_match(rejected.pk, user=owner)

    propose_refund_matches(user=owner, data_key=KEY)

    rejected.refresh_from_db()
    assert rejected.status == ReconciliationMatch.Status.REJECTED
    assert ReconciliationMatch.objects.filter(user=owner).count() == 1


def test_detection_never_reaches_another_users_rows(owner: Any) -> None:
    seed_refund(owner)
    stranger = make_user(email="refund-stranger@example.com")

    assert propose_refund_matches(user=stranger, data_key=KEY) == ()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_refund_reduces_spending_and_is_never_income() -> None:
    refund = CanonicalTransaction.TransactionType.REFUND

    assert is_spending_reduction(refund)
    assert not is_income(refund)
    # Kept out of the spending set so a caller cannot sum a purchase and a
    # refund into a larger total by accident.
    assert not is_spending(refund)


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_a_confirmed_refund_takes_the_purchase_category(owner: Any) -> None:
    purchase, refund, _ = seed_refund(owner)
    clothing = make_category(owner)
    purchase.category_guess = clothing
    purchase.save(update_fields=["category_guess"])
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    event = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert event.transaction_type == CanonicalTransaction.TransactionType.REFUND
    assert event.category_id == clothing.pk
    refund.refresh_from_db()
    assert refund.canonical_transaction_id == event.pk
    assert refund.review_status == ImportedObservation.ReviewStatus.ACCEPTED


def test_a_confirmed_category_outranks_the_parser_guess(owner: Any) -> None:
    purchase, _, account = seed_refund(owner)
    guessed = make_category(owner, "guessed")
    confirmed = make_category(owner, "confirmed")
    purchase.category_guess = guessed
    purchase.save(update_fields=["category_guess"])
    transaction = accept_observation(
        purchase.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    transaction.category = confirmed
    transaction.save(update_fields=["category"])
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    event = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert event.category_id == confirmed.pk


def test_a_refund_never_becomes_income(owner: Any) -> None:
    seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    event = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert event.transaction_type != CanonicalTransaction.TransactionType.INCOME
    assert not is_income(event.transaction_type)


def test_confirming_a_refund_twice_returns_the_same_event(owner: Any) -> None:
    seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    first = confirm_refund_match(match.pk, user=owner, data_key=KEY)
    second = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert first.pk == second.pk
    assert CanonicalTransaction.objects.filter(user=owner).count() == 1


def test_a_refund_already_accepted_alone_blocks_confirmation(owner: Any) -> None:
    _, refund, account = seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]
    accept_observation(
        refund.pk,
        user=owner,
        data_key=KEY,
        financial_account=account,
        transaction_type=CanonicalTransaction.TransactionType.INCOME,
    )

    with pytest.raises(ConflictError):
        confirm_refund_match(match.pk, user=owner, data_key=KEY)


def test_a_rejected_candidate_cannot_become_a_refund(owner: Any) -> None:
    seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]
    reject_match(match.pk, user=owner)

    with pytest.raises(ConflictError):
        confirm_refund_match(match.pk, user=owner, data_key=KEY)


def test_the_plain_confirm_path_refuses_refunds(owner: Any) -> None:
    """Confirming there would leave the credit row acceptable as income."""

    seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    with pytest.raises(ReconciliationError):
        confirm_match(match.pk, user=owner)

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.PROPOSED


def test_another_users_refund_cannot_be_confirmed(owner: Any) -> None:
    seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]
    intruder = make_user(email="refund-intruder@example.com")

    with pytest.raises(ForbiddenError):
        confirm_refund_match(match.pk, user=intruder, data_key=KEY)


def test_the_refund_merchant_is_readable_rather_than_a_copied_envelope(owner: Any) -> None:
    """Ciphertext is bound to its own row, so it cannot be moved across models."""

    _, refund, _ = seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    event = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    assert event.merchant_encrypted == MERCHANT
    assert event.merchant_encrypted != refund.merchant_raw_encrypted


def test_confirming_one_pairing_answers_the_competing_ones(owner: Any) -> None:
    """One refund can resemble several purchases; picking one settles the rest."""

    _, refund, account = seed_refund(owner)
    document = make_document(owner, file_sha256="4" * 64)
    run = make_ocr_run(owner, document)
    twin = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("hyundai_card", "1.0"),
            ParserSupport(0.95, "card_transaction_list", ()),
            (parsed(occurred_on=date(2026, 8, 12)),),
        ),
        data_key=KEY,
        key_version=1,
    ).observations[0]
    twin.financial_account_guess = account
    twin.save(update_fields=["financial_account_guess"])

    candidates = propose_refund_matches(user=owner, data_key=KEY)
    assert len(candidates) == 2

    confirm_refund_match(candidates[0].pk, user=owner, data_key=KEY)

    other = ReconciliationMatch.objects.get(pk=candidates[1].pk)
    assert other.status == ReconciliationMatch.Status.REJECTED
    # And the answered pairing is not asked again on the next detection run.
    propose_refund_matches(user=owner, data_key=KEY)
    other.refresh_from_db()
    assert other.status == ReconciliationMatch.Status.REJECTED
    assert refund.pk not in {row.pk for row in unmatched_refunds(owner)}


def test_confirming_posts_the_ledger_in_the_same_transaction(owner: Any) -> None:
    _, _, account = seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]
    context = make_ledger_accounts(owner, account, prefix="rf")

    event = confirm_refund_match(
        match.pk,
        user=owner,
        data_key=KEY,
        ledger_accounts=PostingRuleAccounts(account=context.account, offset=context.offset),
    )

    assert event.status == CanonicalTransaction.Status.CONFIRMED
    assert LedgerEntry.objects.filter(transaction=event).count() == 2


def test_confirmation_is_audited_without_recording_any_value(owner: Any) -> None:
    purchase, refund, _ = seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    event = confirm_refund_match(match.pk, user=owner, data_key=KEY)

    record = AuditEvent.objects.filter(user=owner, event_type="refund_matched").first()
    assert record is not None
    assert record.metadata["canonical_transaction_id"] == str(event.pk)
    assert record.metadata["refund_observation_id"] == str(refund.pk)
    assert record.metadata["purchase_observation_id"] == str(purchase.pk)
    assert "200000" not in str(record.metadata)
    assert MERCHANT not in str(record.metadata)


# ---------------------------------------------------------------------------
# Unmatched refunds stay visible
# ---------------------------------------------------------------------------


def test_a_credit_no_purchase_claims_stays_listed(owner: Any) -> None:
    _, refund, _ = seed_refund(owner)

    listed = list(unmatched_refunds(owner))

    assert [row.pk for row in listed] == [refund.pk]


def test_a_claimed_refund_leaves_the_unmatched_list(owner: Any) -> None:
    seed_refund(owner)

    propose_refund_matches(user=owner, data_key=KEY)

    assert list(unmatched_refunds(owner)) == []


def test_a_rejected_candidate_returns_its_refund_to_the_unmatched_list(owner: Any) -> None:
    """A dismissed pairing does not make the credit row disappear."""

    _, refund, _ = seed_refund(owner)
    match = propose_refund_matches(user=owner, data_key=KEY)[0]

    reject_match(match.pk, user=owner)

    assert [row.pk for row in unmatched_refunds(owner)] == [refund.pk]


def test_unmatched_refunds_never_include_another_users_rows(owner: Any) -> None:
    seed_refund(owner)
    stranger = make_user(email="refund-onlooker@example.com")

    assert list(unmatched_refunds(stranger)) == []


def test_an_unmatched_refund_is_still_in_the_review_queue(owner: Any) -> None:
    _, refund, _ = seed_refund(owner)

    page = review_queue(owner)

    assert refund.pk in {item.observation.pk for item in page.items}


def test_a_refund_candidate_surfaces_under_its_own_queue_filter(owner: Any) -> None:
    purchase, refund, _ = seed_refund(owner)
    propose_refund_matches(user=owner, data_key=KEY)

    match_ids = queue_match_ids(owner)
    page = review_queue(owner, filters=[QueueFilter.REFUND], match_ids=match_ids)

    assert {item.observation.pk for item in page.items} == {purchase.pk, refund.pk}
    assert page.counts["refund"] == 2
