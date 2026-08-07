from datetime import date
from typing import Any

import pytest

from apps.core.errors import InvalidRequestError
from apps.financial_accounts.models import FinancialAccount
from apps.ledger.models import ChartOfAccounts, LedgerAccount, LedgerEntry
from apps.ledger.rules import (
    PostingRuleAccounts,
    build_transaction_postings,
    post_transaction_by_type,
)
from apps.transactions.models import CanonicalTransaction


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def ledger_context(user: Any) -> dict[str, Any]:
    bank = FinancialAccount.objects.create(
        user=user,
        name_encrypted="bank",
        name_blind_index="rules-bank",
        institution_encrypted="institution",
        institution_blind_index="rules-institution",
        account_type=FinancialAccount.AccountType.CHECKING,
    )
    destination = FinancialAccount.objects.create(
        user=user,
        name_encrypted="destination",
        name_blind_index="rules-destination",
        institution_encrypted="institution",
        institution_blind_index="rules-destination-institution",
        account_type=FinancialAccount.AccountType.SAVINGS,
    )
    chart = ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="personal",
        name_blind_index="rules-chart",
    )

    def account(
        code: str,
        name: str,
        account_type: str,
        normal_balance: str,
        financial_account: FinancialAccount | None = None,
    ) -> LedgerAccount:
        return LedgerAccount.objects.create(
            user=user,
            chart=chart,
            code=code,
            name_encrypted=name,
            name_blind_index=f"rules-{name}",
            account_type=account_type,
            normal_balance=normal_balance,
            financial_account=financial_account,
        )

    return {
        "bank": account(
            "1100",
            "bank",
            LedgerAccount.AccountType.ASSET,
            LedgerAccount.NormalBalance.DEBIT,
            bank,
        ),
        "destination": account(
            "1200",
            "destination",
            LedgerAccount.AccountType.ASSET,
            LedgerAccount.NormalBalance.DEBIT,
            destination,
        ),
        "expense": account(
            "5100",
            "expense",
            LedgerAccount.AccountType.EXPENSE,
            LedgerAccount.NormalBalance.DEBIT,
        ),
        "income": account(
            "4100",
            "income",
            LedgerAccount.AccountType.INCOME,
            LedgerAccount.NormalBalance.CREDIT,
        ),
        "cash": account(
            "1010",
            "cash",
            LedgerAccount.AccountType.ASSET,
            LedgerAccount.NormalBalance.DEBIT,
        ),
        "liability": account(
            "2100",
            "liability",
            LedgerAccount.AccountType.LIABILITY,
            LedgerAccount.NormalBalance.CREDIT,
        ),
    }


def make_transaction(
    user: Any, account: LedgerAccount, transaction_type: str
) -> CanonicalTransaction:
    if account.financial_account is None:
        raise ValueError("The test account must link to a financial account.")
    financial_account = account.financial_account
    return CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=financial_account,
        occurred_at=date(2026, 8, 7),
        amount_encrypted="30000:KRW",
        currency="KRW",
        transaction_type=transaction_type,
        status=CanonicalTransaction.Status.CONFIRMED,
    )


@pytest.mark.django_db
def test_purchase_rule_debits_expense_and_credits_account(
    user: Any, ledger_context: dict[str, Any]
) -> None:
    transaction = make_transaction(
        user, ledger_context["bank"], CanonicalTransaction.TransactionType.PURCHASE
    )
    postings = build_transaction_postings(
        transaction,
        PostingRuleAccounts(account=ledger_context["bank"], offset=ledger_context["expense"]),
    )

    assert [(posting.account, posting.entry_type) for posting in postings] == [
        (ledger_context["expense"], LedgerEntry.EntryType.DEBIT),
        (ledger_context["bank"], LedgerEntry.EntryType.CREDIT),
    ]


@pytest.mark.django_db
def test_income_rule_debits_account_and_credits_income(
    user: Any,
    ledger_context: dict[str, Any],
) -> None:
    transaction = make_transaction(
        user, ledger_context["bank"], CanonicalTransaction.TransactionType.INCOME
    )
    postings = build_transaction_postings(
        transaction,
        PostingRuleAccounts(account=ledger_context["bank"], offset=ledger_context["income"]),
    )

    assert postings[0].entry_type == LedgerEntry.EntryType.DEBIT
    assert postings[1].entry_type == LedgerEntry.EntryType.CREDIT
    assert postings[1].account == ledger_context["income"]


@pytest.mark.django_db
def test_transfer_and_card_payment_rules_use_their_special_accounts(
    user: Any,
    ledger_context: dict[str, Any],
) -> None:
    transfer = make_transaction(
        user, ledger_context["bank"], CanonicalTransaction.TransactionType.INTERNAL_TRANSFER
    )
    transfer_postings = build_transaction_postings(
        transfer,
        PostingRuleAccounts(
            account=ledger_context["bank"],
            transfer_account=ledger_context["destination"],
        ),
    )
    assert transfer_postings[0].account == ledger_context["destination"]
    assert transfer_postings[1].account == ledger_context["bank"]

    payment = make_transaction(
        user, ledger_context["bank"], CanonicalTransaction.TransactionType.CREDIT_CARD_PAYMENT
    )
    payment_postings = build_transaction_postings(
        payment,
        PostingRuleAccounts(
            account=ledger_context["bank"],
            liability_account=ledger_context["liability"],
        ),
    )
    assert payment_postings[0].account == ledger_context["liability"]
    assert payment_postings[0].entry_type == LedgerEntry.EntryType.DEBIT


@pytest.mark.django_db
def test_rule_service_posts_entries_and_rejects_unknown_types(
    user: Any,
    ledger_context: dict[str, Any],
) -> None:
    transaction = make_transaction(
        user, ledger_context["bank"], CanonicalTransaction.TransactionType.PURCHASE
    )
    entries = post_transaction_by_type(
        transaction,
        PostingRuleAccounts(account=ledger_context["bank"], offset=ledger_context["expense"]),
    )
    assert len(entries) == 2

    unknown = make_transaction(user, ledger_context["destination"], "unknown")
    with pytest.raises(InvalidRequestError, match="No ledger posting rule"):
        build_transaction_postings(
            unknown,
            PostingRuleAccounts(account=ledger_context["destination"]),
        )
