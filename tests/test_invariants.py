from __future__ import annotations

from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Money
from apps.ledger.models import ChartOfAccounts, LedgerAccount, LedgerEntry
from apps.ledger.posting import serialize_money
from apps.transactions.invariants import validate_transaction_invariants
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction

from .factories import make_account, make_transaction, make_user


@pytest.mark.django_db
def test_transaction_status_transitions_are_explicit_and_audited() -> None:
    user = make_user(email="status@example.com")
    account = make_account(user)
    transaction = make_transaction(user, account)

    confirmed = transition_transaction_status(
        transaction.pk, user=user, status=CanonicalTransaction.Status.CONFIRMED
    )
    assert confirmed.status == CanonicalTransaction.Status.CONFIRMED
    assert confirmed.reviewed_by_id == user.pk
    with pytest.raises(ConflictError):
        transition_transaction_status(
            transaction.pk, user=user, status=CanonicalTransaction.Status.DRAFT
        )
    assert user.audit_events.filter(event_type="transaction_accepted").exists()


@pytest.mark.django_db
def test_invariants_reject_malformed_amounts_and_unconfirmed_postings() -> None:
    user = make_user(email="invariants@example.com")
    account = make_account(user)
    transaction = make_transaction(user, account, amount_encrypted="not-money")
    with pytest.raises(ValidationError):
        validate_transaction_invariants(transaction)

    transaction.amount_encrypted = "100:KRW"
    transaction.status = CanonicalTransaction.Status.DRAFT
    transaction.save(update_fields=["amount_encrypted", "status"])
    chart = ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="chart",
        name_blind_index="invariant-chart",
    )
    ledger_account = LedgerAccount.objects.create(
        chart=chart,
        user=user,
        code="1000",
        name_encrypted="cash",
        name_blind_index="cash",
        account_type=LedgerAccount.AccountType.ASSET,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
    )
    # The invalid ledger row is created directly to verify the invariant boundary.
    LedgerEntry.objects.create(
        transaction=transaction,
        account=ledger_account,
        entry_type=LedgerEntry.EntryType.DEBIT,
        amount_encrypted=serialize_money(Money(100, "KRW")),
        currency="KRW",
    )
    with pytest.raises(ValidationError, match="confirmed"):
        validate_transaction_invariants(transaction)


@pytest.mark.django_db
def test_manual_creation_is_atomic_when_related_validation_fails() -> None:
    user = make_user(email="atomic@example.com")
    other = make_user(email="other-atomic@example.com")
    other_account = make_account(other, name_blind_index="other-checking")

    with pytest.raises(InvalidRequestError):
        create_manual_transaction(
            user=user,
            occurred_at=date(2026, 8, 7),
            amount_minor=100,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=other_account,
        )
    assert CanonicalTransaction.objects.filter(user=user).count() == 0
