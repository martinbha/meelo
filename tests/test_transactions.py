from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.categorization.models import Category
from apps.financial_accounts.models import FinancialAccount
from apps.transactions.models import CanonicalTransaction


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
