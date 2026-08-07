from datetime import date
from typing import Any

import pytest

from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Money
from apps.financial_accounts.models import FinancialAccount
from apps.ledger.models import ChartOfAccounts, LedgerAccount, LedgerEntry
from apps.ledger.posting import Posting, post_balanced_transaction
from apps.transactions.models import CanonicalTransaction


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def posted_transaction(user: Any) -> tuple[CanonicalTransaction, LedgerAccount, LedgerAccount]:
    financial_account = FinancialAccount.objects.create(
        user=user,
        name_encrypted="checking",
        name_blind_index="posting-checking",
        institution_encrypted="institution",
        institution_blind_index="posting-institution",
        account_type=FinancialAccount.AccountType.CHECKING,
    )
    chart = ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="personal",
        name_blind_index="posting-chart",
    )
    expense = LedgerAccount.objects.create(
        user=user,
        chart=chart,
        code="5000",
        name_encrypted="food",
        name_blind_index="food-ledger",
        account_type=LedgerAccount.AccountType.EXPENSE,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
    )
    bank = LedgerAccount.objects.create(
        user=user,
        chart=chart,
        code="1100",
        name_encrypted="bank",
        name_blind_index="bank-ledger",
        account_type=LedgerAccount.AccountType.ASSET,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
    )
    transaction = CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=financial_account,
        occurred_at=date(2026, 8, 7),
        amount_encrypted="30000:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        status=CanonicalTransaction.Status.CONFIRMED,
    )
    return transaction, expense, bank


@pytest.mark.django_db
def test_balanced_posting_creates_debit_and_credit_entries(
    posted_transaction: tuple[CanonicalTransaction, LedgerAccount, LedgerAccount],
) -> None:
    transaction, expense, bank = posted_transaction
    entries = post_balanced_transaction(
        transaction,
        [
            Posting(expense, LedgerEntry.EntryType.DEBIT, Money(30000, "KRW")),
            Posting(bank, LedgerEntry.EntryType.CREDIT, Money(30000, "KRW")),
        ],
    )

    assert len(entries) == 2
    assert {entry.entry_type for entry in entries} == {"debit", "credit"}
    assert all(entry.amount_encrypted == "30000:KRW" for entry in entries)


@pytest.mark.django_db
def test_unbalanced_posting_is_rejected_without_entries(
    posted_transaction: tuple[CanonicalTransaction, LedgerAccount, LedgerAccount],
) -> None:
    transaction, expense, bank = posted_transaction

    with pytest.raises(InvalidRequestError, match="equal"):
        post_balanced_transaction(
            transaction,
            [
                Posting(expense, LedgerEntry.EntryType.DEBIT, Money(30000, "KRW")),
                Posting(bank, LedgerEntry.EntryType.CREDIT, Money(29999, "KRW")),
            ],
        )

    assert not LedgerEntry.objects.filter(transaction=transaction).exists()


@pytest.mark.django_db
def test_transaction_can_only_be_posted_once(
    posted_transaction: tuple[CanonicalTransaction, LedgerAccount, LedgerAccount],
) -> None:
    transaction, expense, bank = posted_transaction
    postings = [
        Posting(expense, LedgerEntry.EntryType.DEBIT, Money(30000, "KRW")),
        Posting(bank, LedgerEntry.EntryType.CREDIT, Money(30000, "KRW")),
    ]
    post_balanced_transaction(transaction, postings)

    with pytest.raises(ConflictError, match="already been posted"):
        post_balanced_transaction(transaction, postings)
