from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.categorization.models import Category
from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    provision_user_data_key,
)
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.observations.models import ImportedObservation
from apps.processing.models import SourceDocument
from apps.reconciliation.models import ReconciliationMatch
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction
from tests.factories import make_user

pytestmark = pytest.mark.django_db

PRIVATE_LABEL = "Other owner's secret checking"
PRIVATE_INDEXES = (
    "acct_idx_not_a_name",
    "inst_idx_not_a_name",
    "category_idx_not_a_name",
)


@pytest.fixture
def selector_data(tmp_path: Path, settings: Any) -> dict[str, Any]:
    master_key = os.urandom(32)
    master_path = tmp_path / "master.key"
    master_path.write_text(base64.urlsafe_b64encode(master_key).decode(), encoding="ascii")
    master_path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(master_path)
    settings.FIELD_ENCRYPTION_REQUIRED = True
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")

    owner = make_user(email="selector-owner@example.com", password="password")
    stranger = make_user(email="selector-stranger@example.com", password="password")
    for user in (owner, stranger):
        provision_user_data_key(user=user, actor=user, master_key=master_key)
    owner_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    owner_search_key = get_user_search_key(user=owner, actor=owner, master_key=master_key)
    stranger_key = get_user_data_key(user=stranger, actor=stranger, master_key=master_key)

    account = FinancialAccount(
        user=owner,
        name_encrypted="",
        name_blind_index=PRIVATE_INDEXES[0],
        institution_encrypted="",
        institution_blind_index="bank_idx_not_a_name",
        account_type=FinancialAccount.AccountType.CHECKING,
        identifier_last_four="4321",
        currency="KRW",
    )
    account.encrypt_fields(
        {
            "name_encrypted": "Daily Spending",
            "institution_encrypted": "Synthetic Bank",
            "opening_balance_encrypted": "0:KRW",
        },
        key=owner_key,
        key_version=owner.encryption_key_version,
    )
    account.full_clean()
    account.save()

    card = PaymentInstrument(
        user=owner,
        name_encrypted="",
        name_blind_index=PRIVATE_INDEXES[1],
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        last_four="1234",
        financial_account=account,
        issuer_encrypted="",
    )
    card.encrypt_fields(
        {"name_encrypted": "Travel Debit", "issuer_encrypted": "Synthetic Bank"},
        key=owner_key,
        key_version=owner.encryption_key_version,
    )
    card.full_clean()
    card.save()

    parent = Category(
        user=owner,
        name_encrypted="",
        name_blind_index="parent_idx_not_a_name",
        category_type=Category.CategoryType.EXPENSE,
    )
    parent.encrypt_fields({"name_encrypted": "Household"}, key=owner_key)
    parent.full_clean()
    parent.save()
    category = Category(
        user=owner,
        name_encrypted="",
        name_blind_index=PRIVATE_INDEXES[2],
        parent=parent,
        category_type=Category.CategoryType.EXPENSE,
    )
    category.encrypt_fields({"name_encrypted": "Coffee"}, key=owner_key)
    category.full_clean()
    category.save()

    stranger_account = FinancialAccount(
        user=stranger,
        name_encrypted="",
        name_blind_index="stranger_account_idx",
        institution_encrypted="",
        institution_blind_index="stranger_bank_idx",
        account_type=FinancialAccount.AccountType.CHECKING,
    )
    stranger_account.encrypt_fields(
        {"name_encrypted": PRIVATE_LABEL, "institution_encrypted": "Stranger Bank"},
        key=stranger_key,
    )
    stranger_account.save()
    stranger_category = Category(
        user=stranger,
        name_encrypted="",
        name_blind_index="stranger_category_idx",
        category_type=Category.CategoryType.EXPENSE,
    )
    stranger_category.encrypt_fields({"name_encrypted": PRIVATE_LABEL}, key=stranger_key)
    stranger_category.save()

    return {
        "master_key": master_key,
        "owner": owner,
        "owner_key": owner_key,
        "owner_search_key": owner_search_key,
        "stranger": stranger,
        "account": account,
        "card": card,
        "parent": parent,
        "category": category,
        "stranger_account": stranger_account,
        "stranger_category": stranger_category,
    }


def _two_observations(data: dict[str, Any]) -> tuple[ImportedObservation, ImportedObservation]:
    owner = data["owner"]
    document = SourceDocument.objects.create(
        user=owner,
        file_sha256=os.urandom(32).hex(),
        original_filename_encrypted="first.png",
        mime_type="image/png",
        file_size=16,
    )
    other_document = SourceDocument.objects.create(
        user=owner,
        file_sha256=os.urandom(32).hex(),
        original_filename_encrypted="second.png",
        mime_type="image/png",
        file_size=16,
    )
    rows = []
    for index, (source, source_document) in enumerate(
        (("Cafe One", document), ("Cafe Two", other_document))
    ):
        observation = ImportedObservation.objects.create(
            user=owner,
            source_document=source_document,
            row_index=index,
            occurred_at=date(2026, 8, 15 + index),
            currency="KRW",
            direction=ImportedObservation.Direction.DEBIT,
            merchant_blind_index=f"observation_idx_{index}_not_a_name",
        )
        observation.encrypt_fields(
            {
                "merchant_raw_encrypted": source,
                "amount_encrypted": f"{4_200 + index * 100}:KRW",
            },
            key=data["owner_key"],
            key_version=owner.encryption_key_version,
        )
        observation.save(update_fields=["merchant_raw_encrypted", "amount_encrypted"])
        rows.append(observation)
    return rows[0], rows[1]


def test_manual_transaction_selectors_render_names_keep_ids_and_exclude_other_users(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    owner = data["owner"]
    account = data["account"]
    card = data["card"]
    category = data["category"]
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("transaction-new"))

    assert response.status_code == 200
    html = response.content.decode()
    for label in (
        "Daily Spending",
        "Synthetic Bank",
        "Checking",
        "ending 4321",
        "Travel Debit",
        "Debit card",
        "ending 1234",
        "Household › Coffee",
    ):
        assert label in html
    assert PRIVATE_LABEL not in html
    assert all(index not in html for index in (*PRIVATE_INDEXES, "stranger_account_idx"))
    assert f'value="{account.pk}"' in html
    assert f'value="{card.pk}"' in html
    assert f'value="{category.pk}"' in html

    response = client.post(
        reverse("transaction-new"),
        {
            "occurred_at": "2026-08-15",
            "amount": "42.00",
            "currency": "USD",
            "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
            "financial_account": str(account.pk),
            "payment_instrument": str(card.pk),
            "category": str(category.pk),
            "merchant": "Cafe",
        },
    )

    assert response.status_code == 302
    transaction = CanonicalTransaction.objects.get(user=owner)
    assert transaction.financial_account_id == account.pk
    assert transaction.payment_instrument_id == card.pk
    assert transaction.category_id == category.pk


def test_observation_mapping_choices_are_readable_and_owner_scoped(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    owner = data["owner"]
    observation, _ = _two_observations(data)
    observation.financial_account_guess = data["account"]
    observation.payment_instrument_guess = data["card"]
    observation.category_guess = data["category"]
    observation.save(
        update_fields=[
            "financial_account_guess",
            "payment_instrument_guess",
            "category_guess",
            "updated_at",
        ]
    )
    client = Client()
    client.force_login(owner)

    response = client.get(
        reverse("observation-review", kwargs={"pk": observation.source_document_id})
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Daily Spending" in html
    assert "Travel Debit" in html
    assert "Household › Coffee" in html
    assert PRIVATE_LABEL not in html
    assert all(index not in html for index in PRIVATE_INDEXES)
    assert f'value="{data["account"].pk}"' in html
    assert f'value="{data["card"].pk}"' in html
    assert f'value="{data["category"].pk}"' in html

    response = client.post(
        reverse("observation-action", kwargs={"pk": observation.pk, "action": "correct"}),
        {
            "financial_account_guess": str(data["account"].pk),
            "payment_instrument_guess": str(data["card"].pk),
            "category_guess": str(data["category"].pk),
        },
    )

    assert response.status_code == 302
    observation.refresh_from_db()
    assert observation.financial_account_guess_id == data["account"].pk
    assert observation.payment_instrument_guess_id == data["card"].pk
    assert observation.category_guess_id == data["category"].pk


def test_category_correction_choice_uses_hierarchy_and_stable_id(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    owner = data["owner"]
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=4_200,
        currency="USD",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=data["account"],
        merchant="Cafe",
        data_key=data["owner_key"],
        blind_index_key=data["owner_search_key"],
        key_version=owner.encryption_key_version,
    )
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("transaction-category", kwargs={"pk": transaction.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Household › Coffee" in html
    assert PRIVATE_LABEL not in html
    assert PRIVATE_INDEXES[2] not in html
    assert f'value="{data["category"].pk}"' in html

    response = client.post(
        reverse("transaction-category", kwargs={"pk": transaction.pk}),
        {
            "category": str(data["category"].pk),
            "scope": "transaction_only",
            "apply_to_existing": "",
        },
    )

    assert response.status_code == 302
    transaction.refresh_from_db()
    assert transaction.category_id == data["category"].pk


def test_manual_reconciliation_choices_show_readable_observation_context(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    owner = data["owner"]
    first, second = _two_observations(data)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("match-link"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Cafe One" in html
    assert "Cafe Two" in html
    assert "2026-08-15" in html
    assert "₩4,200" in html
    assert PRIVATE_LABEL not in html
    assert f'value="{first.pk}"' in html
    assert f'value="{second.pk}"' in html

    response = client.post(
        reverse("match-link"),
        {
            "left_observation": str(first.pk),
            "right_observation": str(second.pk),
            "match_type": ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        },
    )

    assert response.status_code == 302
    assert ReconciliationMatch.objects.filter(
        user=owner,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
    ).exists()


def test_unreadable_observation_values_use_safe_reconciliation_labels(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    owner = data["owner"]
    first, _ = _two_observations(data)
    first.merchant_raw_encrypted = "v1.1.invalid.envelope.value"
    first.amount_encrypted = "v1.1.invalid.envelope.value"
    first.save(update_fields=["merchant_raw_encrypted", "amount_encrypted"])
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("match-link"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Merchant unavailable" in html
    assert "Amount unavailable" in html
    assert "v1." not in html
    assert first.merchant_blind_index not in html


@pytest.mark.parametrize(
    ("route", "forbidden_text"),
    [
        ("report-accounts", "stranger_account_idx"),
        ("report-cards", "stranger_account_idx"),
        ("report-categories", "stranger_category_idx"),
    ],
)
def test_report_filters_use_readable_labels_and_only_owned_values(
    selector_data: dict[str, Any], route: str, forbidden_text: str
) -> None:
    data = selector_data
    client = Client()
    client.force_login(data["owner"])
    params: dict[str, str] = {"year": "2026", "month": "8"}
    if route == "report-accounts":
        params["account"] = str(data["account"].pk)
    elif route == "report-cards":
        params["instrument"] = str(data["card"].pk)
    else:
        params["category"] = str(data["category"].pk)

    response = client.get(reverse(route), params)

    assert response.status_code == 200
    html = response.content.decode()
    assert "Daily Spending" in html or route == "report-categories"
    assert "Travel Debit" in html or route != "report-cards"
    assert "Household › Coffee" in html or route != "report-categories"
    assert PRIVATE_LABEL not in html
    assert forbidden_text not in html
    assert PRIVATE_INDEXES[0] not in html
    assert PRIVATE_INDEXES[1] not in html
    assert PRIVATE_INDEXES[2] not in html
    if route == "report-accounts":
        assert response.context["selected_account"] == data["account"].pk
        assert f'value="{data["account"].pk}"' in html
    elif route == "report-cards":
        assert response.context["selected_instrument"] == data["card"].pk
        assert f'value="{data["card"].pk}"' in html
    else:
        assert response.context["selected_category"] == data["category"].pk
        assert f'value="{data["category"].pk}"' in html


def test_unreadable_account_names_use_a_safe_contextual_label(
    selector_data: dict[str, Any],
) -> None:
    data = selector_data
    account = data["account"]
    account.name_encrypted = "v1.1.invalid.envelope.value"
    account.save(update_fields=["name_encrypted", "updated_at"])
    client = Client()
    client.force_login(data["owner"])

    response = client.get(reverse("transaction-new"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Account name unavailable" in html
    assert "Checking" in html
    assert "ending 4321" in html
    assert "v1." not in html
    assert PRIVATE_INDEXES[0] not in html


@pytest.mark.parametrize("nested", [False, True])
def test_envelope_shaped_plaintext_names_are_distinguished_from_authenticated_nested_values(
    selector_data: dict[str, Any], nested: bool
) -> None:
    data = selector_data
    account = data["account"]
    literal_name = "v1.1.release.plan.name"
    value = account.name_encrypted if nested else literal_name
    account.encrypt_fields({"name_encrypted": value}, key=data["owner_key"])
    account.save(update_fields=["name_encrypted", "updated_at"])
    client = Client()
    client.force_login(data["owner"])

    response = client.get(reverse("transaction-new"))

    assert response.status_code == 200
    html = response.content.decode()
    if nested:
        assert "Account name unavailable" in html
        assert value not in html
    else:
        assert literal_name in html
        assert "Account name unavailable" not in html
