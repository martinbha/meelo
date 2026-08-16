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


def make_document(user: Any, **overrides: Any) -> Any:
    from apps.processing.models import SourceDocument

    values: dict[str, Any] = {
        "user": user,
        "file_sha256": "9" * 64,
        "original_filename_encrypted": "encrypted",
        "mime_type": "image/png",
        "file_size": 1024,
        "image_width": 1080,
        "image_height": 1920,
        "source_type": SourceDocument.SourceType.BANK_TRANSACTION_LIST,
    }
    values.update(overrides)
    return SourceDocument.objects.create(**values)


def make_ocr_run(user: Any, document: Any, **overrides: Any) -> Any:
    from django.utils import timezone

    from apps.ocr.models import OcrRun

    now = timezone.now()
    values: dict[str, Any] = {
        "user": user,
        "source_document": document,
        "engine": "primary",
        "engine_version": "1",
        "languages": ["ko"],
        "configuration_encrypted": "config",
        "succeeded": True,
        "duration_ms": 5,
        "started_at": now,
        "completed_at": now,
    }
    values.update(overrides)
    return OcrRun.objects.create(**values)


def make_ledger_accounts(user: Any, account: FinancialAccount, *, prefix: str = "f") -> Any:
    """A minimal chart with an asset account for ``account`` and an expense offset."""

    from apps.ledger.models import ChartOfAccounts, LedgerAccount
    from apps.ledger.rules import PostingRuleAccounts

    chart = ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="personal",
        name_blind_index=f"{prefix}-chart",
    )
    asset = LedgerAccount.objects.create(
        user=user,
        chart=chart,
        code=f"{prefix}-1000",
        name_encrypted="bank",
        name_blind_index=f"{prefix}-bank",
        account_type=LedgerAccount.AccountType.ASSET,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
        financial_account=account,
    )
    expense = LedgerAccount.objects.create(
        user=user,
        chart=chart,
        code=f"{prefix}-5000",
        name_encrypted="expense",
        name_blind_index=f"{prefix}-expense",
        account_type=LedgerAccount.AccountType.EXPENSE,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
    )
    return PostingRuleAccounts(account=asset, offset=expense)


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
