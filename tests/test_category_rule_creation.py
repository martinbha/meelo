"""Turning a correction into a rule, at a scope the user picks (#83, spec 18).

Re-filing a coffee shop tells the system something reusable. How far it reuses
it is the user's decision, and the failure this file guards against is the
system deciding for them: one correction quietly reclassifying a year.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.categorization.engine import CategorySource
from apps.categorization.models import Category, CategoryRule
from apps.categorization.normalization import merchant_blind_index
from apps.categorization.rule_creation import (
    RuleScope,
    create_rule_from_correction,
    preview_rule,
)
from apps.core.errors import InvalidRequestError
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.models import AuditEvent
from apps.instruments.models import PaymentInstrument
from apps.observations.models import ImportedObservation
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)
MERCHANT = "스타벅스 강남점"


@pytest.fixture
def owner() -> Any:
    return make_user(email="rule-owner@example.com")


def make_category(user: Any, name: str) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"rule-{name}",
        category_type=Category.CategoryType.EXPENSE,
    )


def index(user: Any, value: str = MERCHANT) -> str:
    return merchant_blind_index(value, user_id=user.pk, key=KEY)


def make_instrument(user: Any, account: Any, name: str = "card") -> PaymentInstrument:
    return PaymentInstrument.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"rule-{name}",
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
        "merchant_encrypted": MERCHANT,
        "merchant_blind_index": index(user),
    }
    values.update(overrides)
    return CanonicalTransaction.objects.create(**values)


def make_observation(user: Any, **overrides: Any) -> ImportedObservation:
    document = make_document(user, file_sha256=os.urandom(32).hex())
    values: dict[str, Any] = {
        "user": user,
        "source_document": document,
        "ocr_run": make_ocr_run(user, document),
        "occurred_at": date(2026, 8, 14),
        "currency": "KRW",
        "direction": ImportedObservation.Direction.DEBIT,
        "merchant_blind_index": index(user),
    }
    values.update(overrides)
    return ImportedObservation.objects.create(**values)


# ---------------------------------------------------------------------------
# The preview, before anything is written
# ---------------------------------------------------------------------------


def test_a_preview_writes_nothing(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)

    preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT)

    assert not CategoryRule.objects.filter(user=owner).exists()
    transaction.refresh_from_db()
    assert transaction.category_id is None


def test_the_preview_counts_what_the_rule_would_reach(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    make_observation(owner)
    make_observation(owner)
    make_transaction(owner, account)

    preview = preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT)

    assert preview.pending_observations == 2
    # Both drafts: the one being corrected and the other from the same merchant.
    assert preview.draft_transactions == 2
    assert preview.writes_a_rule


def test_the_preview_shows_the_history_that_stays_as_it_is(owner: Any) -> None:
    """A user choosing a scope should see what will not move."""

    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    make_transaction(owner, account, status=CanonicalTransaction.Status.CONFIRMED)
    make_transaction(owner, account, category_source=CategorySource.MANUAL_OVERRIDE)

    preview = preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT)

    assert preview.confirmed_transactions == 1
    assert preview.manually_categorized == 1
    # Neither is offered up for reclassification.
    assert preview.draft_transactions == 1


def test_a_card_scope_counts_only_that_card(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    instrument = make_instrument(owner, account)
    other = make_instrument(owner, account, name="other-card")
    transaction = make_transaction(owner, account, payment_instrument=instrument)
    make_transaction(owner, account, payment_instrument=other)

    narrow = preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT_AND_CARD)
    wide = preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT)

    assert narrow.draft_transactions == 1
    assert wide.draft_transactions == 2


def test_the_transaction_only_scope_reaches_nothing_else(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    make_observation(owner)

    preview = preview_rule(user=owner, transaction=transaction, scope=RuleScope.TRANSACTION_ONLY)

    assert preview.touches_nothing_yet
    assert not preview.writes_a_rule


def test_a_card_scope_without_a_card_is_refused(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        preview_rule(user=owner, transaction=transaction, scope=RuleScope.MERCHANT_AND_CARD)


def test_an_unknown_scope_is_refused(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)

    with pytest.raises(InvalidRequestError):
        preview_rule(user=owner, transaction=transaction, scope="everything_forever")


# ---------------------------------------------------------------------------
# Creating the rule
# ---------------------------------------------------------------------------


def correct(owner: Any, transaction: Any, category: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "user": owner,
        "transaction": transaction,
        "category": category,
        "scope": RuleScope.MERCHANT,
        "merchant": MERCHANT,
        "encryption_key": KEY,
        "blind_index_key": KEY,
    }
    values.update(overrides)
    return create_rule_from_correction(**values)


def test_the_transaction_only_scope_writes_no_rule(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    coffee = make_category(owner, "coffee")

    result = correct(owner, transaction, coffee, scope=RuleScope.TRANSACTION_ONLY)

    assert result.rule is None
    assert not CategoryRule.objects.filter(user=owner).exists()
    transaction.refresh_from_db()
    assert transaction.category_id == coffee.pk
    assert transaction.category_source == CategorySource.MANUAL_OVERRIDE


def test_the_merchant_scope_writes_an_unscoped_rule(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    instrument = make_instrument(owner, account)
    transaction = make_transaction(owner, account, payment_instrument=instrument)
    coffee = make_category(owner, "coffee")

    result = correct(owner, transaction, coffee, scope=RuleScope.MERCHANT)

    assert result.rule is not None
    assert result.rule.payment_instrument_id is None
    assert result.rule.category_id == coffee.pk


def test_the_card_scope_writes_a_rule_bound_to_that_card(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    instrument = make_instrument(owner, account)
    transaction = make_transaction(owner, account, payment_instrument=instrument)
    coffee = make_category(owner, "coffee")

    result = correct(owner, transaction, coffee, scope=RuleScope.MERCHANT_AND_CARD)

    assert result.rule is not None
    assert result.rule.payment_instrument_id == instrument.pk


def test_a_new_rule_leaves_existing_transactions_alone_by_default(owner: Any) -> None:
    """A rule applies to what comes next unless the user asks otherwise."""

    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    existing = make_transaction(owner, account)
    coffee = make_category(owner, "coffee")

    result = correct(owner, transaction, coffee)

    existing.refresh_from_db()
    assert result.reclassified == 0
    assert existing.category_id is None


def test_applying_backwards_updates_only_unconfirmed_rows(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    draft = make_transaction(owner, account)
    confirmed = make_transaction(owner, account, status=CanonicalTransaction.Status.CONFIRMED)
    coffee = make_category(owner, "coffee")

    result = correct(owner, transaction, coffee, apply_to_existing=True)

    draft.refresh_from_db()
    confirmed.refresh_from_db()
    assert result.reclassified == 1
    assert draft.category_id == coffee.pk
    assert draft.category_source == CategorySource.USER_RULE
    # Confirmed history is never rewritten.
    assert confirmed.category_id is None


def test_applying_backwards_leaves_hand_corrected_rows_alone(owner: Any) -> None:
    """A rule written a minute ago does not overrule a deliberate decision."""

    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)
    chosen = make_category(owner, "gifts")
    by_hand = make_transaction(
        owner, account, category=chosen, category_source=CategorySource.MANUAL_OVERRIDE
    )

    correct(owner, transaction, make_category(owner, "coffee"), apply_to_existing=True)

    by_hand.refresh_from_db()
    assert by_hand.category_id == chosen.pk


def test_rule_creation_is_audited_with_its_scope(owner: Any) -> None:
    account = make_account(owner, name_blind_index="rule-account")
    transaction = make_transaction(owner, account)

    correct(owner, transaction, make_category(owner, "coffee"), scope=RuleScope.MERCHANT)

    events = [
        event.metadata
        for event in AuditEvent.objects.filter(user=owner, event_type="category_rule_created")
    ]
    assert any(item.get("scope") == "merchant" for item in events)
    assert any(item.get("source") == "review_correction" for item in events)


def test_another_users_transaction_cannot_be_corrected(owner: Any) -> None:
    stranger = make_user(email="rule-stranger@example.com")
    account = make_account(stranger, name_blind_index="rule-theirs")
    transaction = make_transaction(stranger, account)

    with pytest.raises(InvalidRequestError):
        correct(owner, transaction, make_category(owner, "coffee"))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def web_owner(master_key: bytes) -> Any:
    user = make_user(email="rule-web@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


def test_the_page_shows_a_preview_for_every_scope(web_owner: Any, master_key: bytes) -> None:
    account = make_account(web_owner, name_blind_index="rule-web-account")
    instrument = make_instrument(web_owner, account, name="web-card")
    data_key = get_user_data_key(user=web_owner, actor=web_owner, master_key=master_key)
    transaction = make_transaction(
        web_owner,
        account,
        payment_instrument=instrument,
        merchant_blind_index=merchant_blind_index(MERCHANT, user_id=web_owner.pk, key=data_key),
    )
    client = Client()
    client.force_login(web_owner)

    response = client.get(reverse("transaction-category", kwargs={"pk": transaction.pk}))

    assert response.status_code == 200
    assert len(response.context["previews"]) == len(RuleScope)


def test_the_page_refuses_a_post_without_a_scope(web_owner: Any) -> None:
    account = make_account(web_owner, name_blind_index="rule-web-account")
    transaction = make_transaction(web_owner, account)
    category = make_category(web_owner, "coffee")
    client = Client()
    client.force_login(web_owner)

    response = client.post(
        reverse("transaction-category", kwargs={"pk": transaction.pk}),
        data={"category": str(category.pk)},
    )

    assert response.status_code == 400
    transaction.refresh_from_db()
    assert transaction.category_id is None


def test_another_users_transaction_is_not_reachable(web_owner: Any) -> None:
    stranger = make_user(email="rule-web-stranger@example.com")
    account = make_account(stranger, name_blind_index="rule-web-theirs")
    transaction = make_transaction(stranger, account)
    client = Client()
    client.force_login(web_owner)

    response = client.get(reverse("transaction-category", kwargs={"pk": transaction.pk}))

    assert response.status_code == 404
