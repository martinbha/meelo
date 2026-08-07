from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.categorization.models import Category, CategoryRule, MerchantAlias
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def category(user: Any) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted="food",
        name_blind_index="food-index",
        category_type=Category.CategoryType.EXPENSE,
    )


def make_account(user: Any) -> FinancialAccount:
    return FinancialAccount.objects.create(
        user=user,
        name_encrypted="checking",
        name_blind_index="checking-index",
        institution_encrypted="institution",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
    )


@pytest.mark.django_db
def test_merchant_alias_can_set_default_category_and_card_scope(
    user: Any, category: Category
) -> None:
    account = make_account(user)
    instrument = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="debit",
        name_blind_index="debit-index",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        financial_account=account,
    )
    alias = MerchantAlias.objects.create(
        user=user,
        alias_encrypted="raw merchant",
        alias_blind_index="raw-merchant-index",
        normalized_merchant_encrypted="merchant",
        normalized_merchant_blind_index="merchant-index",
        default_category=category,
        payment_instrument=instrument,
    )

    assert alias.default_category_id == category.pk
    assert alias.payment_instrument_id == instrument.pk


@pytest.mark.django_db
def test_category_rule_matches_merchant_and_optional_scopes(user: Any, category: Category) -> None:
    rule = CategoryRule.objects.create(
        user=user,
        merchant_pattern_encrypted="merchant",
        merchant_pattern_blind_index="merchant-index",
        category=category,
        priority=10,
    )

    assert rule.matches("merchant-index") is True
    assert rule.matches("other-index") is False
    rule.is_active = False
    assert rule.matches("merchant-index") is False


@pytest.mark.django_db
def test_alias_related_records_must_belong_to_same_user(user: Any, category: Category) -> None:
    other = type(user).objects.create_user("other@example.com", password="password")
    other_category = Category.objects.create(
        user=other,
        name_encrypted="other",
        name_blind_index="other-index",
        category_type=Category.CategoryType.EXPENSE,
    )
    alias = MerchantAlias(
        user=user,
        alias_encrypted="raw",
        alias_blind_index="raw-index",
        normalized_merchant_encrypted="normalized",
        normalized_merchant_blind_index="normalized-index",
        default_category=other_category,
    )

    with pytest.raises(ValidationError, match="same user"):
        alias.full_clean()
