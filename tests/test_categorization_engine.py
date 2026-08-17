"""The categorization priority engine (#81, specification 18).

The order the sources are tried in is the whole design. A rule the user wrote
beats a name the system learned; anything the user has already corrected beats
every guess; and when nothing applies the answer is *uncategorized* rather than
somewhere plausible — a wrong category that looks confident is worse than an
empty one, because only the empty one gets fixed.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest

from apps.categorization.engine import (
    PRECEDENCE,
    CategorySource,
    classify,
)
from apps.categorization.models import Category, CategoryRule, MerchantAlias
from apps.categorization.services import (
    categorize_transaction,
    create_exact_merchant_rule,
    create_merchant_alias,
    merchant_blind_index,
    set_category_manually,
    set_rule_active,
)
from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.models import AuditEvent
from apps.instruments.models import PaymentInstrument
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

ENCRYPTION_KEY = os.urandom(32)
BLIND_INDEX_KEY = os.urandom(32)

MERCHANT = "스타벅스 강남점"
COUNTERPARTY = "김대성"


@pytest.fixture
def owner() -> Any:
    return make_user(email="engine-owner@example.com")


def make_category(user: Any, name: str) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"engine-{name}",
        category_type=Category.CategoryType.EXPENSE,
    )


def index(user: Any, value: str) -> str:
    return merchant_blind_index(value, user_id=user.pk, key=BLIND_INDEX_KEY)


def make_instrument(user: Any, account: Any, name: str = "card") -> PaymentInstrument:
    return PaymentInstrument.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"engine-{name}",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        financial_account=account,
    )


def make_transaction(user: Any, account: Any, **overrides: Any) -> CanonicalTransaction:
    values: dict[str, Any] = {
        "user": user,
        "created_by": user,
        "financial_account": account,
        "occurred_at": date(2026, 8, 14),
        "amount_encrypted": "4200:KRW",
        "merchant_encrypted": "encrypted",
        "merchant_blind_index": index(user, MERCHANT),
    }
    values.update(overrides)
    return CanonicalTransaction.objects.create(**values)


# ---------------------------------------------------------------------------
# The order itself
# ---------------------------------------------------------------------------


def test_the_precedence_is_the_one_the_specification_names() -> None:
    assert PRECEDENCE == (
        CategorySource.MANUAL_OVERRIDE,
        CategorySource.USER_RULE,
        CategorySource.CARD_RULE,
        CategorySource.MERCHANT_ALIAS,
        CategorySource.COUNTERPARTY_RULE,
        CategorySource.PRIOR_CONFIRMATION,
        CategorySource.PARSER,
        CategorySource.UNCATEGORIZED,
    )


def test_nothing_matching_leaves_the_transaction_visibly_uncategorized(owner: Any) -> None:
    """Saying so is the honest answer; a plausible guess would never be fixed."""

    account = make_account(owner, name_blind_index="engine-account")
    transaction = make_transaction(owner, account)

    decision = classify(transaction, user=owner)

    assert decision.category is None
    assert decision.source == CategorySource.UNCATEGORIZED
    assert not decision.is_categorized


def test_a_user_rule_beats_a_card_rule_an_alias_and_a_counterparty_rule(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    instrument = make_instrument(owner, account)
    winner = make_category(owner, "from-rule")

    create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=winner,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    CategoryRule.objects.create(
        user=owner,
        category=make_category(owner, "from-card"),
        rule_type=CategoryRule.RuleType.PAYMENT_INSTRUMENT,
        payment_instrument=instrument,
        merchant_pattern_encrypted="",
        merchant_pattern_blind_index="",
    )
    create_merchant_alias(
        user=owner,
        alias=MERCHANT,
        normalized_merchant=MERCHANT,
        default_category=make_category(owner, "from-alias"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account, payment_instrument=instrument)

    decision = classify(transaction, user=owner)

    assert decision.category == winner
    assert decision.source == CategorySource.USER_RULE


def test_a_card_rule_beats_an_alias(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    instrument = make_instrument(owner, account)
    winner = make_category(owner, "from-card")
    CategoryRule.objects.create(
        user=owner,
        category=winner,
        rule_type=CategoryRule.RuleType.PAYMENT_INSTRUMENT,
        payment_instrument=instrument,
        merchant_pattern_encrypted="",
        merchant_pattern_blind_index="",
    )
    create_merchant_alias(
        user=owner,
        alias=MERCHANT,
        normalized_merchant=MERCHANT,
        default_category=make_category(owner, "from-alias"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account, payment_instrument=instrument)

    decision = classify(transaction, user=owner)

    assert decision.category == winner
    assert decision.source == CategorySource.CARD_RULE


def test_an_alias_beats_a_counterparty_rule(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    winner = make_category(owner, "from-alias")
    create_merchant_alias(
        user=owner,
        alias=MERCHANT,
        normalized_merchant=MERCHANT,
        default_category=winner,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    CategoryRule.objects.create(
        user=owner,
        category=make_category(owner, "from-counterparty"),
        rule_type=CategoryRule.RuleType.COUNTERPARTY_EXACT,
        merchant_pattern_encrypted="",
        merchant_pattern_blind_index=index(owner, COUNTERPARTY),
    )
    transaction = make_transaction(
        owner, account, counterparty_blind_index=index(owner, COUNTERPARTY)
    )

    decision = classify(transaction, user=owner)

    assert decision.category == winner
    assert decision.source == CategorySource.MERCHANT_ALIAS


def test_a_counterparty_rule_beats_a_prior_confirmation(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    winner = make_category(owner, "from-counterparty")
    CategoryRule.objects.create(
        user=owner,
        category=winner,
        rule_type=CategoryRule.RuleType.COUNTERPARTY_EXACT,
        merchant_pattern_encrypted="",
        merchant_pattern_blind_index=index(owner, COUNTERPARTY),
    )
    earlier = make_transaction(owner, account, occurred_at=date(2026, 7, 1))
    set_category_manually(
        transaction_id=earlier.pk, user=owner, category=make_category(owner, "from-history")
    )
    transaction = make_transaction(
        owner, account, counterparty_blind_index=index(owner, COUNTERPARTY)
    )

    decision = classify(transaction, user=owner)

    assert decision.category == winner
    assert decision.source == CategorySource.COUNTERPARTY_RULE


def test_a_prior_confirmation_beats_the_parser_guess(owner: Any) -> None:
    """The user filed this merchant once; they did not state a policy."""

    account = make_account(owner, name_blind_index="engine-account")
    chosen = make_category(owner, "from-history")
    earlier = make_transaction(owner, account, occurred_at=date(2026, 7, 1))
    set_category_manually(transaction_id=earlier.pk, user=owner, category=chosen)
    transaction = make_transaction(owner, account, category=make_category(owner, "from-parser"))

    decision = classify(transaction, user=owner)

    assert decision.category == chosen
    assert decision.source == CategorySource.PRIOR_CONFIRMATION


def test_the_most_recent_confirmation_is_the_one_that_counts(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    older = make_transaction(owner, account, occurred_at=date(2026, 6, 1))
    newer = make_transaction(owner, account, occurred_at=date(2026, 7, 1))
    set_category_manually(transaction_id=older.pk, user=owner, category=make_category(owner, "old"))
    latest = make_category(owner, "new")
    set_category_manually(transaction_id=newer.pk, user=owner, category=latest)
    transaction = make_transaction(owner, account, occurred_at=date(2026, 8, 1))

    assert classify(transaction, user=owner).category == latest


def test_the_parser_guess_survives_when_no_rule_speaks(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    guessed = make_category(owner, "from-parser")
    transaction = make_transaction(owner, account, category=guessed)

    decision = classify(transaction, user=owner)

    assert decision.category == guessed
    assert decision.source == CategorySource.PARSER


def test_a_disabled_rule_does_not_decide_anything(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    rule = create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=make_category(owner, "disabled"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    set_rule_active(user=owner, rule_id=rule.pk, is_active=False)
    transaction = make_transaction(owner, account)

    assert classify(transaction, user=owner).source == CategorySource.UNCATEGORIZED


def test_another_users_rule_never_decides_anything(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    stranger = make_user(email="engine-stranger@example.com")
    create_exact_merchant_rule(
        user=stranger,
        merchant=MERCHANT,
        category=make_category(stranger, "theirs"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)

    assert classify(transaction, user=owner).source == CategorySource.UNCATEGORIZED


def test_classification_is_deterministic_across_repeated_runs(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=make_category(owner, "food"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)

    answers = {
        (classify(transaction, user=owner).category, classify(transaction, user=owner).source)
        for _ in range(5)
    }

    assert len(answers) == 1


# ---------------------------------------------------------------------------
# Applying and overriding
# ---------------------------------------------------------------------------


def test_applying_stores_both_the_category_and_its_source(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    food = make_category(owner, "food")
    rule = create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=food,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)

    decision = categorize_transaction(transaction_id=transaction.pk, user=owner)

    transaction.refresh_from_db()
    assert decision.rule_id == rule.pk
    assert transaction.category_id == food.pk
    assert transaction.category_source == CategorySource.USER_RULE


def test_a_manual_correction_survives_reclassification(owner: Any) -> None:
    """Re-running would otherwise undo the user's work on a schedule."""

    account = make_account(owner, name_blind_index="engine-account")
    create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=make_category(owner, "food"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)
    chosen = make_category(owner, "gifts")
    set_category_manually(transaction_id=transaction.pk, user=owner, category=chosen)

    decision = categorize_transaction(transaction_id=transaction.pk, user=owner)

    transaction.refresh_from_db()
    assert decision.source == CategorySource.MANUAL_OVERRIDE
    assert transaction.category_id == chosen.pk


def test_a_manual_correction_is_allowed_on_a_confirmed_transaction(owner: Any) -> None:
    """The category is the part of a confirmed row a person keeps refining."""

    account = make_account(owner, name_blind_index="engine-account")
    transaction = make_transaction(owner, account, status=CanonicalTransaction.Status.CONFIRMED)
    chosen = make_category(owner, "gifts")

    set_category_manually(transaction_id=transaction.pk, user=owner, category=chosen)

    transaction.refresh_from_db()
    assert transaction.category_id == chosen.pk
    # But automatic classification still refuses to touch it.
    with pytest.raises(ConflictError):
        categorize_transaction(transaction_id=transaction.pk, user=owner)


def test_clearing_a_category_by_hand_returns_it_to_uncategorized(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    transaction = make_transaction(owner, account, category=make_category(owner, "wrong"))

    set_category_manually(transaction_id=transaction.pk, user=owner, category=None)

    transaction.refresh_from_db()
    assert transaction.category_id is None
    assert transaction.category_source == CategorySource.UNCATEGORIZED


def test_a_manual_correction_is_audited_without_naming_the_category(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    transaction = make_transaction(owner, account)
    chosen = make_category(owner, "secret-habit")

    set_category_manually(transaction_id=transaction.pk, user=owner, category=chosen)

    event = AuditEvent.objects.filter(user=owner, event_type="category_changed").first()
    assert event is not None
    assert event.metadata["category_id"] == str(chosen.pk)
    # A category name is the user's own words about their spending.
    assert "secret-habit" not in str(event.metadata)


def test_another_users_category_cannot_be_assigned(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    transaction = make_transaction(owner, account)
    stranger = make_user(email="engine-thief@example.com")

    with pytest.raises(InvalidRequestError):
        set_category_manually(
            transaction_id=transaction.pk, user=owner, category=make_category(stranger, "theirs")
        )


def test_reapplying_the_same_decision_writes_nothing(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    create_exact_merchant_rule(
        user=owner,
        merchant=MERCHANT,
        category=make_category(owner, "food"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)
    categorize_transaction(transaction_id=transaction.pk, user=owner)
    transaction.refresh_from_db()
    stamp = transaction.updated_at

    categorize_transaction(transaction_id=transaction.pk, user=owner)

    transaction.refresh_from_db()
    assert transaction.updated_at == stamp


def test_an_alias_without_a_default_category_decides_nothing(owner: Any) -> None:
    account = make_account(owner, name_blind_index="engine-account")
    create_merchant_alias(
        user=owner,
        alias=MERCHANT,
        normalized_merchant=MERCHANT,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    transaction = make_transaction(owner, account)

    assert MerchantAlias.objects.filter(user=owner).exists()
    assert classify(transaction, user=owner).source == CategorySource.UNCATEGORIZED
