from __future__ import annotations

from datetime import date
from typing import Any

from apps.financial_accounts.models import FinancialAccount
from apps.transactions.models import CanonicalTransaction


def make_user(**overrides: Any) -> Any:
    from django.contrib.auth import get_user_model

    values = {"email": "factory@example.com", "password": "password"}
    values.update(overrides)
    return get_user_model().objects.create_user(**values)


def make_account(user: Any, **overrides: Any) -> FinancialAccount:
    values: dict[str, Any] = {
        "user": user,
        "name_encrypted": "checking",
        "name_blind_index": "factory-checking",
        "institution_encrypted": "bank",
        "institution_blind_index": "factory-bank",
        "account_type": FinancialAccount.AccountType.CHECKING,
    }
    values.update(overrides)
    return FinancialAccount.objects.create(**values)


def make_transaction(
    user: Any, account: FinancialAccount, **overrides: Any
) -> CanonicalTransaction:
    values: dict[str, Any] = {
        "user": user,
        "created_by": user,
        "financial_account": account,
        "occurred_at": date(2026, 8, 7),
        "amount_encrypted": "100:KRW",
        "merchant_encrypted": "merchant",
        "merchant_blind_index": "factory-merchant",
        "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
    }
    values.update(overrides)
    transaction = CanonicalTransaction(**values)
    transaction.full_clean()
    transaction.save()
    return transaction
