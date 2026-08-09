from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.categorization.models import Category
from apps.core.audit import record_audit_event
from apps.core.errors import InvalidRequestError
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.ledger.models import LedgerEntry
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction, update_manual_transaction


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def account(user: Any) -> FinancialAccount:
    return FinancialAccount.objects.create(
        user=user,
        name_encrypted="checking",
        name_blind_index="checking-index",
        institution_encrypted="institution",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
    )


def make_transaction(
    user: Any,
    account: FinancialAccount,
    **overrides: Any,
) -> CanonicalTransaction:
    values: dict[str, Any] = {
        "user": user,
        "created_by": user,
        "financial_account": account,
        "occurred_at": date(2026, 8, 7),
        "amount_encrypted": "42900:KRW",
        "merchant_encrypted": "merchant",
        "merchant_blind_index": "merchant-index",
        "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
    }
    values.update(overrides)
    return CanonicalTransaction(**values)


@pytest.mark.django_db
def test_canonical_transaction_links_review_and_reporting_records(
    user: Any,
    account: FinancialAccount,
) -> None:
    category = Category.objects.create(
        user=user,
        name_encrypted="food",
        name_blind_index="food-index",
        category_type=Category.CategoryType.EXPENSE,
    )
    transaction = make_transaction(user, account, category=category, status="confirmed")
    transaction.full_clean()
    transaction.save()

    assert transaction.pk is not None
    assert transaction.category_id == category.pk
    assert transaction.status == CanonicalTransaction.Status.CONFIRMED


@pytest.mark.django_db
def test_transaction_rejects_impossible_posted_date(
    user: Any,
    account: FinancialAccount,
) -> None:
    transaction = make_transaction(
        user,
        account,
        posted_at=date(2026, 8, 6),
    )

    with pytest.raises(ValidationError, match="earlier"):
        transaction.full_clean()


@pytest.mark.django_db
def test_transaction_rejects_related_records_from_other_user(
    user: Any,
    account: FinancialAccount,
) -> None:
    other = type(user).objects.create_user("other@example.com", password="password")
    other_account = FinancialAccount.objects.create(
        user=other,
        name_encrypted="other",
        name_blind_index="other-index",
        institution_encrypted="institution",
        institution_blind_index="other-institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
    )
    transaction = make_transaction(user, account, financial_account=other_account)

    with pytest.raises(ValidationError, match="same user"):
        transaction.full_clean()


@pytest.mark.django_db
def test_manual_transaction_is_draft_and_audited(user: Any, account: FinancialAccount) -> None:
    transaction = create_manual_transaction(
        user=user,
        occurred_at=date(2026, 8, 7),
        amount_minor=42900,
        currency="krw",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant="Cafe",
    )

    assert transaction.status == CanonicalTransaction.Status.DRAFT
    assert transaction.amount_encrypted == "42900:KRW"
    assert LedgerEntry.objects.filter(transaction=transaction).count() == 0
    assert user.audit_events.filter(event_type="transaction_created").exists()


@pytest.mark.django_db
def test_manual_transaction_rejects_incompatible_card(user: Any, account: FinancialAccount) -> None:
    other_account = FinancialAccount.objects.create(
        user=user,
        name_encrypted="savings",
        name_blind_index="savings-index",
        institution_encrypted="bank",
        institution_blind_index="bank-index",
        account_type=FinancialAccount.AccountType.SAVINGS,
    )
    card = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="card",
        name_blind_index="card-index",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        financial_account=other_account,
    )

    with pytest.raises(InvalidRequestError, match="compatible"):
        create_manual_transaction(
            user=user,
            occurred_at=date(2026, 8, 7),
            amount_minor=100,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
            payment_instrument=card,
        )


@pytest.mark.django_db
def test_transaction_correction_is_audited(user: Any, account: FinancialAccount) -> None:
    transaction = create_manual_transaction(
        user=user,
        occurred_at=date(2026, 8, 7),
        amount_minor=100,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
    )
    update_manual_transaction(
        transaction.pk,
        user=user,
        occurred_at=date(2026, 8, 8),
        amount_minor=200,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.FEE,
        financial_account=account,
    )

    assert user.audit_events.filter(event_type="transaction_corrected").count() == 1


@pytest.mark.django_db
def test_audit_events_are_chained_without_plaintext(user: Any) -> None:
    first = record_audit_event(user=user, event_type="login_success", metadata={"object_id": "x"})
    second = record_audit_event(user=user, event_type="logout")

    assert first.previous_digest == ""
    assert second.previous_digest == first.digest
    assert "secret" not in second.metadata
