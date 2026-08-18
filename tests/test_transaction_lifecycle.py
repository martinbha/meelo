"""Confirmed history is not rewritten silently (#152, specification 18, 25.2).

Three claims, each tested against the behaviour rather than the field:

- an illegal transition raises and leaves the row exactly as it was,
- a confirmed transaction cannot be edited by the ordinary edit path, only
  through a correction that says what changed,
- a voided transaction leaves the reports.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Money
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import Posting, post_balanced_transaction
from apps.reports.spending import monthly_spending, reportable_transactions
from apps.transactions.lifecycle import (
    ALLOWED_STATUS_TRANSITIONS,
    ImmutableTransactionError,
    TransitionError,
    correct_confirmed_transaction,
    transition_transaction_status,
)
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import update_manual_transaction
from tests.factories import make_account, make_ledger_accounts, make_transaction, make_user

pytestmark = pytest.mark.django_db

DRAFT = CanonicalTransaction.Status.DRAFT
CONFIRMED = CanonicalTransaction.Status.CONFIRMED
VOIDED = CanonicalTransaction.Status.VOIDED


@pytest.fixture
def owner() -> Any:
    return make_user(email="lifecycle@example.com")


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner)


def confirmed_transaction(owner: Any, account: Any, **overrides: Any) -> CanonicalTransaction:
    transaction = make_transaction(owner, account, **overrides)
    return transition_transaction_status(transaction.pk, user=owner, status=CONFIRMED)


# ----------------------------------------------------------------------
# Transitions
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (DRAFT, DRAFT),
        (CONFIRMED, DRAFT),
        (CONFIRMED, CONFIRMED),
        (VOIDED, DRAFT),
        (VOIDED, CONFIRMED),
        (VOIDED, VOIDED),
    ],
)
def test_an_illegal_transition_raises_and_changes_nothing(
    owner: Any, account: Any, start: str, target: str
) -> None:
    transaction = make_transaction(owner, account)
    if start != DRAFT:
        transition_transaction_status(transaction.pk, user=owner, status=start)
    before = CanonicalTransaction.objects.get(pk=transaction.pk)

    with pytest.raises(TransitionError):
        transition_transaction_status(transaction.pk, user=owner, status=target)

    after = CanonicalTransaction.objects.get(pk=transaction.pk)
    assert after.status == before.status == start
    assert after.updated_at == before.updated_at


def test_a_status_that_is_not_a_status_is_refused(owner: Any, account: Any) -> None:
    """A typo must not be read as an unlisted transition and allowed through."""

    transaction = make_transaction(owner, account)
    with pytest.raises(TransitionError):
        transition_transaction_status(transaction.pk, user=owner, status="approved")
    assert CanonicalTransaction.objects.get(pk=transaction.pk).status == DRAFT


def test_the_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_STATUS_TRANSITIONS) == set(CanonicalTransaction.Status.values)
    assert ALLOWED_STATUS_TRANSITIONS[VOIDED] == frozenset()


def test_voiding_is_audited_as_a_void_rather_than_a_deletion(owner: Any, account: Any) -> None:
    transaction = make_transaction(owner, account)
    transition_transaction_status(transaction.pk, user=owner, status=VOIDED)

    event = owner.audit_events.filter(event_type="transaction_voided").get()
    assert event.metadata == {"status": VOIDED, "previous_status": DRAFT}
    assert not owner.audit_events.filter(event_type="transaction_deleted").exists()


def test_a_transition_cannot_reach_another_users_transaction(owner: Any, account: Any) -> None:
    intruder = make_user(email="lifecycle-intruder@example.com")
    transaction = make_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        transition_transaction_status(transaction.pk, user=intruder, status=CONFIRMED)
    assert CanonicalTransaction.objects.get(pk=transaction.pk).status == DRAFT


# ----------------------------------------------------------------------
# Editing confirmed history
# ----------------------------------------------------------------------


def test_a_confirmed_transaction_cannot_be_edited_in_place(owner: Any, account: Any) -> None:
    """Confirmation is the line, not posting.

    The previous rule refused edits only once ledger entries existed, which left
    a window: a confirmed transaction reports had already counted could still be
    rewritten with nothing recording that it had ever said something else.
    """

    transaction = confirmed_transaction(owner, account)
    assert not transaction.ledger_entries.exists()

    with pytest.raises(ImmutableTransactionError):
        update_manual_transaction(
            transaction.pk,
            user=owner,
            occurred_at=date(2026, 8, 7),
            amount_minor=999,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
        )

    assert CanonicalTransaction.objects.get(pk=transaction.pk).amount_encrypted == "100:KRW"


def test_a_voided_transaction_cannot_be_edited_at_all(owner: Any, account: Any) -> None:
    transaction = make_transaction(owner, account)
    transition_transaction_status(transaction.pk, user=owner, status=VOIDED)

    with pytest.raises(ImmutableTransactionError):
        update_manual_transaction(
            transaction.pk,
            user=owner,
            occurred_at=date(2026, 8, 7),
            amount_minor=999,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
        )


def test_a_draft_is_still_edited_normally(owner: Any, account: Any) -> None:
    transaction = make_transaction(owner, account)

    updated = update_manual_transaction(
        transaction.pk,
        user=owner,
        occurred_at=date(2026, 8, 9),
        amount_minor=250,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
    )

    assert updated.amount_encrypted == "250:KRW"
    assert updated.status == DRAFT


# ----------------------------------------------------------------------
# The correction path
# ----------------------------------------------------------------------


def test_a_correction_records_which_fields_changed_and_why(owner: Any, account: Any) -> None:
    transaction = confirmed_transaction(owner, account)

    corrected, changed = correct_confirmed_transaction(
        transaction.pk,
        user=owner,
        reason="The receipt is dated the day before the statement.",
        occurred_at=date(2026, 8, 6),
    )

    assert changed == ("occurred_at",)
    assert corrected.occurred_at == date(2026, 8, 6)
    assert corrected.status == CONFIRMED

    event = owner.audit_events.filter(event_type="transaction_corrected").get()
    assert event.metadata["changed_fields"] == ["occurred_at"]
    assert event.metadata["reason"].startswith("The receipt is dated")


def test_a_correction_records_names_and_never_values(owner: Any, account: Any) -> None:
    """Specification 23: audit logs must not contain financial plaintext."""

    transaction = confirmed_transaction(owner, account)
    other_account = make_account(owner, name_blind_index="lifecycle-second")

    correct_confirmed_transaction(
        transaction.pk,
        user=owner,
        reason="Paid from the other account.",
        financial_account=other_account,
    )

    event = owner.audit_events.filter(event_type="transaction_corrected").get()
    assert event.metadata["changed_fields"] == ["financial_account"]
    assert str(other_account.pk) not in str(event.metadata)


def test_a_correction_that_changes_nothing_records_nothing(owner: Any, account: Any) -> None:
    transaction = confirmed_transaction(owner, account)

    _, changed = correct_confirmed_transaction(
        transaction.pk, user=owner, reason="No change.", occurred_at=transaction.occurred_at
    )

    assert changed == ()
    assert not owner.audit_events.filter(event_type="transaction_corrected").exists()


def test_a_correction_requires_a_reason(owner: Any, account: Any) -> None:
    transaction = confirmed_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        correct_confirmed_transaction(
            transaction.pk, user=owner, reason="   ", occurred_at=date(2026, 8, 6)
        )
    assert CanonicalTransaction.objects.get(pk=transaction.pk).occurred_at == date(2026, 8, 7)


def test_an_unknown_field_is_refused_rather_than_ignored(owner: Any, account: Any) -> None:
    """A silently dropped keyword would report a correction that never happened."""

    transaction = confirmed_transaction(owner, account)

    with pytest.raises(InvalidRequestError, match="cannot be corrected"):
        correct_confirmed_transaction(
            transaction.pk, user=owner, reason="Typo.", merchant_encrypted="somewhere else"
        )


def test_only_a_confirmed_transaction_is_corrected(owner: Any, account: Any) -> None:
    draft = make_transaction(owner, account)

    with pytest.raises(ImmutableTransactionError):
        correct_confirmed_transaction(
            draft.pk, user=owner, reason="Wrong date.", occurred_at=date(2026, 8, 6)
        )


def test_a_correction_cannot_reach_another_users_transaction(owner: Any, account: Any) -> None:
    intruder = make_user(email="correction-intruder@example.com")
    transaction = confirmed_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        correct_confirmed_transaction(
            transaction.pk, user=intruder, reason="Mine now.", occurred_at=date(2026, 8, 6)
        )
    assert CanonicalTransaction.objects.get(pk=transaction.pk).occurred_at == date(2026, 8, 7)


def test_a_posted_transaction_refuses_a_correction_the_ledger_would_contradict(
    owner: Any, account: Any
) -> None:
    transaction = confirmed_transaction(owner, account)
    accounts = make_ledger_accounts(owner, account, prefix="lifecycle")
    post_balanced_transaction(
        transaction,
        [
            Posting(accounts.offset, LedgerEntry.EntryType.DEBIT, Money(100, "KRW")),
            Posting(accounts.account, LedgerEntry.EntryType.CREDIT, Money(100, "KRW")),
        ],
    )

    with pytest.raises(ConflictError, match="Reverse it"):
        correct_confirmed_transaction(
            transaction.pk,
            user=owner,
            reason="Wrong account.",
            transaction_type=CanonicalTransaction.TransactionType.FEE,
        )

    # A correction the postings do not depend on still goes through.
    _, changed = correct_confirmed_transaction(
        transaction.pk, user=owner, reason="Statement date.", posted_at=date(2026, 8, 10)
    )
    assert changed == ("posted_at",)


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def test_a_voided_transaction_leaves_the_reports(owner: Any, account: Any) -> None:
    kept = make_transaction(owner, account, amount_encrypted="4200:KRW")
    withdrawn = make_transaction(owner, account, amount_encrypted="9900:KRW")

    before = monthly_spending(owner, year=2026, month=8).by_currency["KRW"]
    assert before.gross_spending_minor == 14_100

    transition_transaction_status(withdrawn.pk, user=owner, status=VOIDED)

    after = monthly_spending(owner, year=2026, month=8).by_currency["KRW"]
    assert after.gross_spending_minor == 4_200
    assert after.transaction_count == 1
    assert list(reportable_transactions(owner)) == [kept]
