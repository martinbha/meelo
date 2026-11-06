from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.categorization.normalization import merchant_blind_index
from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    provision_user_data_key,
)
from apps.core.searchable import counterparty_index
from apps.transactions.forms import ManualTransactionForm
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money
from apps.transactions.services import create_manual_transaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

MERCHANT = "Starbucks Gangnam"
COUNTERPARTY = "Card issuer"
NOTES = "Lunch with a colleague"
CURRENCY = "USD"


@pytest.fixture
def owner_keys(tmp_path: Path, settings: Any) -> tuple[Any, bytes, Any]:
    master_key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(master_key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.FIELD_ENCRYPTION_REQUIRED = True
    owner = make_user(email="encrypted-edit-owner@example.com", password="password")
    provision_user_data_key(user=owner, actor=owner, master_key=master_key)
    return owner, master_key, path


def encrypted_draft(
    owner: Any, *, master_key: bytes
) -> tuple[Any, CanonicalTransaction, bytes, Any]:
    account = make_account(owner, name_blind_index=f"encrypted-edit-{owner.pk}")
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    search_key = get_user_search_key(user=owner, actor=owner, master_key=master_key)
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 7),
        amount_minor=42_900,
        currency=CURRENCY,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant=MERCHANT,
        counterparty=COUNTERPARTY,
        notes=NOTES,
        data_key=data_key,
        blind_index_key=search_key,
        key_version=owner.encryption_key_version,
    )
    return account, transaction, data_key, search_key


def edit_payload(
    account: Any,
    *,
    amount: str = "429.00",
    merchant: str = MERCHANT,
    counterparty: str = COUNTERPARTY,
    notes: str = NOTES,
) -> dict[str, str]:
    return {
        "occurred_at": "2026-08-07",
        "amount": amount,
        "currency": CURRENCY,
        "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
        "financial_account": str(account.pk),
        "payment_instrument": "",
        "category": "",
        "merchant": merchant,
        "counterparty": counterparty,
        "notes": notes,
    }


def test_encrypted_draft_get_and_unchanged_edit_preserve_values_and_indexes(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, data_key, _ = encrypted_draft(owner, master_key=master_key)
    original_indexes = (transaction.merchant_blind_index, transaction.counterparty_blind_index)
    original_ciphertexts = (
        transaction.merchant_encrypted,
        transaction.counterparty_encrypted,
        transaction.notes_encrypted,
    )
    client = Client()
    client.force_login(owner)
    url = reverse("transaction-edit", kwargs={"pk": transaction.pk})

    response = client.get(url)

    assert response.status_code == 200
    html = response.content.decode()
    assert MERCHANT in html
    assert COUNTERPARTY in html
    assert NOTES in html
    assert 'value="429.00"' in html
    assert "v1." not in html
    assert all(ciphertext not in html for ciphertext in original_ciphertexts)

    response = client.post(url, edit_payload(account), follow=True)

    assert response.status_code == 200
    transaction.refresh_from_db()
    assert transaction.read_field("merchant_encrypted", key=data_key) == MERCHANT
    assert transaction.read_field("counterparty_encrypted", key=data_key) == COUNTERPARTY
    assert transaction.read_field("notes_encrypted", key=data_key) == NOTES
    assert read_money(transaction, "amount_encrypted", data_key=data_key).amount_minor == 42_900
    assert (
        transaction.merchant_blind_index,
        transaction.counterparty_blind_index,
    ) == original_indexes


def test_editing_only_notes_preserves_other_plaintext_and_search_indexes(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, data_key, _ = encrypted_draft(owner, master_key=master_key)
    original_indexes = (transaction.merchant_blind_index, transaction.counterparty_blind_index)
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("transaction-edit", kwargs={"pk": transaction.pk}),
        edit_payload(account, notes="Verified against the receipt"),
    )

    assert response.status_code == 302
    transaction.refresh_from_db()
    assert transaction.read_field("merchant_encrypted", key=data_key) == MERCHANT
    assert transaction.read_field("counterparty_encrypted", key=data_key) == COUNTERPARTY
    assert transaction.read_field("notes_encrypted", key=data_key) == "Verified against the receipt"
    assert read_money(transaction, "amount_encrypted", data_key=data_key).amount_minor == 42_900
    assert (
        transaction.merchant_blind_index,
        transaction.counterparty_blind_index,
    ) == original_indexes


def test_another_owner_cannot_open_an_encrypted_edit_form(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, _, _ = encrypted_draft(owner, master_key=master_key)
    stranger = make_user(email="encrypted-edit-stranger@example.com", password="password")
    client = Client()
    client.force_login(stranger)

    response = client.get(reverse("transaction-edit", kwargs={"pk": transaction.pk}))

    assert response.status_code == 404
    transaction.refresh_from_db()
    assert transaction.merchant_blind_index == merchant_blind_index(
        MERCHANT,
        user_id=owner.pk,
        key=get_user_search_key(user=owner, actor=owner, master_key=master_key),
    )
    assert account.user_id == owner.pk


def test_plaintext_starting_with_envelope_prefix_is_not_rewritten_as_ciphertext(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account = make_account(owner, name_blind_index=f"encrypted-edit-prefix-{owner.pk}")
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    search_key = get_user_search_key(user=owner, actor=owner, master_key=master_key)
    merchant = "v1.1.user.entered.memo"
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 7),
        amount_minor=42_900,
        currency=CURRENCY,
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=account,
        merchant=merchant,
        data_key=data_key,
        blind_index_key=search_key,
        key_version=owner.encryption_key_version,
    )
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("transaction-edit", kwargs={"pk": transaction.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert merchant in html
    assert ManualTransactionForm.UNREADABLE_SOURCE_MESSAGE not in html


@pytest.mark.parametrize("nested", [False, True], ids=["corrupt", "nested-envelope"])
def test_unreadable_source_values_are_hidden_and_cannot_be_overwritten(
    owner_keys: tuple[Any, bytes, Path], nested: bool
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, _, _ = encrypted_draft(owner, master_key=master_key)
    if nested:
        nested_envelope = transaction.merchant_encrypted
        transaction.encrypt_fields(
            {"merchant_encrypted": nested_envelope},
            key=get_user_data_key(user=owner, actor=owner, master_key=master_key),
        )
    else:
        transaction.merchant_encrypted = "v1.1.invalid.envelope.value"
    transaction.save(update_fields=["merchant_encrypted", "updated_at"])
    stored_value = transaction.merchant_encrypted
    client = Client()
    client.force_login(owner)
    url = reverse("transaction-edit", kwargs={"pk": transaction.pk})

    response = client.get(url)

    assert response.status_code == 200
    html = response.content.decode()
    assert "One or more saved values could not be read." in html
    assert "v1." not in html
    assert stored_value not in html

    response = client.post(url, edit_payload(account, merchant="replacement"))

    assert response.status_code == 200
    assert "One or more saved values could not be read." in response.content.decode()
    transaction.refresh_from_db()
    assert transaction.merchant_encrypted == stored_value


def test_edit_form_recomputes_indexes_from_decrypted_values_not_envelopes(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, _, search_key = encrypted_draft(owner, master_key=master_key)
    client = Client()
    client.force_login(owner)

    client.post(
        reverse("transaction-edit", kwargs={"pk": transaction.pk}),
        edit_payload(account),
    )

    transaction.refresh_from_db()
    assert transaction.merchant_blind_index == merchant_blind_index(
        MERCHANT, user_id=owner.pk, key=search_key
    )
    assert transaction.counterparty_blind_index == counterparty_index(
        COUNTERPARTY, user_id=owner.pk, key=search_key
    )


def test_authenticated_but_malformed_amount_is_reported_without_overwriting_history(
    owner_keys: tuple[Any, bytes, Path],
) -> None:
    owner, master_key, _ = owner_keys
    account, transaction, data_key, _ = encrypted_draft(owner, master_key=master_key)
    transaction.encrypt_fields({"amount_encrypted": "invalid-money-payload"}, key=data_key)
    transaction.save(update_fields=["amount_encrypted", "updated_at"])
    stored_value = transaction.amount_encrypted
    client = Client()
    client.force_login(owner)
    url = reverse("transaction-edit", kwargs={"pk": transaction.pk})

    for response in (client.get(url), client.post(url, edit_payload(account))):
        assert response.status_code == 200
        assert "One or more saved values could not be read." in response.content.decode()
        assert "invalid-money-payload" not in response.content.decode()
    transaction.refresh_from_db()
    assert transaction.amount_encrypted == stored_value
