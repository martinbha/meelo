import os
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.categorization.engine import CategorySource
from apps.categorization.models import Category, CategoryRule, MerchantAlias
from apps.categorization.services import (
    categorize_transaction,
    create_exact_merchant_rule,
    create_merchant_alias,
    decrypt_normalized_merchant,
    find_merchant_alias,
    merchant_blind_index,
    set_rule_active,
)
from apps.core.errors import ConflictError
from apps.core.models import AuditEvent
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.transactions.models import CanonicalTransaction

ENCRYPTION_KEY = os.urandom(32)
BLIND_INDEX_KEY = os.urandom(32)


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


@pytest.mark.django_db
def test_alias_service_encrypts_values_and_matches_by_scoped_blind_index(
    user: Any, category: Category
) -> None:
    alias = create_merchant_alias(
        user=user,
        alias="  Corner   SHOP ",
        normalized_merchant="Corner Shop Seoul",
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
        default_category=category,
    )

    assert "corner shop" not in alias.alias_encrypted.casefold()
    assert decrypt_normalized_merchant(alias, encryption_key=ENCRYPTION_KEY) == "corner shop seoul"
    assert (
        find_merchant_alias(user=user, merchant="corner shop", blind_index_key=BLIND_INDEX_KEY)
        == alias
    )
    assert (
        AuditEvent.objects.get(user=user).event_type == AuditEvent.EventType.MERCHANT_ALIAS_CREATED
    )
    other = type(user).objects.create_user("alias-other@example.com", password="password")
    assert (
        find_merchant_alias(user=other, merchant="corner shop", blind_index_key=BLIND_INDEX_KEY)
        is None
    )


@pytest.mark.django_db
def test_generic_alias_is_unique_per_user(user: Any, category: Category) -> None:
    values = {
        "user": user,
        "alias": "Corner Shop",
        "normalized_merchant": "Corner Shop",
        "encryption_key": ENCRYPTION_KEY,
        "blind_index_key": BLIND_INDEX_KEY,
        "key_version": 1,
        "default_category": category,
    }
    create_merchant_alias(**values)

    with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
        create_merchant_alias(**values)


@pytest.mark.django_db
def test_card_specific_alias_takes_precedence(user: Any, category: Category) -> None:
    account = make_account(user)
    instrument = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="debit",
        name_blind_index="specific-card",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        financial_account=account,
    )
    generic = create_merchant_alias(
        user=user,
        alias="Cafe",
        normalized_merchant="Generic Cafe",
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
        default_category=category,
    )
    specific = create_merchant_alias(
        user=user,
        alias="Cafe",
        normalized_merchant="Card Cafe",
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
        default_category=category,
        payment_instrument=instrument,
    )

    assert (
        find_merchant_alias(
            user=user,
            merchant="Cafe",
            blind_index_key=BLIND_INDEX_KEY,
            payment_instrument_id=instrument.pk,
        )
        == specific
    )
    assert (
        find_merchant_alias(
            user=user,
            merchant="Cafe",
            blind_index_key=BLIND_INDEX_KEY,
        )
        == generic
    )


@pytest.mark.django_db
def test_rule_lifecycle_is_audited_and_preserves_history(user: Any, category: Category) -> None:
    rule = create_exact_merchant_rule(
        user=user,
        merchant="Cafe",
        category=category,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )

    set_rule_active(user=user, rule_id=rule.pk, is_active=False)
    rule.refresh_from_db()
    assert rule.is_active is False
    assert rule.disabled_at is not None
    assert CategoryRule.objects.filter(pk=rule.pk).exists()
    assert list(AuditEvent.objects.filter(user=user).values_list("event_type", flat=True)) == [
        AuditEvent.EventType.CATEGORY_RULE_CREATED,
        AuditEvent.EventType.CATEGORY_RULE_DISABLED,
    ]

    set_rule_active(user=user, rule_id=rule.pk, is_active=True)
    rule.refresh_from_db()
    assert rule.disabled_at is None


@pytest.mark.django_db
def test_rule_applies_to_draft_but_never_silently_rewrites_confirmed_transaction(
    user: Any, category: Category
) -> None:
    account = make_account(user)
    merchant_index = merchant_blind_index("Cafe", user_id=user.pk, key=BLIND_INDEX_KEY)
    rule = create_exact_merchant_rule(
        user=user,
        merchant="Cafe",
        category=category,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    draft = CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, 8, 14),
        amount_encrypted="100:KRW",
        merchant_encrypted="encrypted-value",
        merchant_blind_index=merchant_index,
    )

    decision = categorize_transaction(transaction_id=draft.pk, user=user)
    assert decision.rule_id == rule.pk
    assert decision.source == CategorySource.USER_RULE
    draft.refresh_from_db()
    assert draft.category_id == category.pk

    draft.status = CanonicalTransaction.Status.CONFIRMED
    draft.category = None
    draft.save(update_fields=("status", "category"))
    with pytest.raises(ConflictError, match="explicit category correction"):
        categorize_transaction(transaction_id=draft.pk, user=user)
    draft.refresh_from_db()
    assert draft.category_id is None


@pytest.mark.django_db
def test_card_scoped_rule_wins_over_generic_rule_at_same_priority(
    user: Any, category: Category
) -> None:
    account = make_account(user)
    instrument = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="card",
        name_blind_index="rule-card",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        financial_account=account,
    )
    card_category = Category.objects.create(
        user=user,
        name_encrypted="card food",
        name_blind_index="card-food-index",
        category_type=Category.CategoryType.EXPENSE,
    )
    create_exact_merchant_rule(
        user=user,
        merchant="Cafe",
        category=category,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    card_rule = create_exact_merchant_rule(
        user=user,
        merchant="Cafe",
        category=card_category,
        payment_instrument=instrument,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction_record = CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        payment_instrument=instrument,
        occurred_at=date(2026, 8, 14),
        amount_encrypted="100:KRW",
        merchant_encrypted="encrypted-value",
        merchant_blind_index=merchant_blind_index("Cafe", user_id=user.pk, key=BLIND_INDEX_KEY),
    )

    decision = categorize_transaction(transaction_id=transaction_record.pk, user=user)
    assert decision.rule_id == card_rule.pk
    transaction_record.refresh_from_db()
    assert transaction_record.category_id == card_category.pk
