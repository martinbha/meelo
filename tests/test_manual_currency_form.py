from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.crypto import is_encrypted_value
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.financial_accounts.models import FinancialAccount
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.FIELD_ENCRYPTION_REQUIRED = True
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="manual-currency@example.com", password="password")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def account(owner: Any) -> FinancialAccount:
    return make_account(owner, name_blind_index="manual-currency-account")


@pytest.mark.parametrize(
    ("currency", "entered", "expected_minor"),
    [
        ("KRW", "42900", 42_900),
        ("USD", "10.25", 1_025),
        ("JPY", "100", 100),
        ("VND", "100", 100),
    ],
)
def test_manual_entry_scales_encrypted_amount_using_currency_exponent(
    owner: Any,
    account: FinancialAccount,
    master_key: bytes,
    currency: str,
    entered: str,
    expected_minor: int,
) -> None:
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("transaction-new"),
        data={
            "occurred_at": "2026-08-15",
            "amount": entered,
            "currency": currency,
            "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
            "financial_account": str(account.pk),
            "merchant": "Synthetic currency test",
        },
    )

    assert response.status_code == 302
    transaction = CanonicalTransaction.objects.get(user=owner)
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    assert is_encrypted_value(transaction.amount_encrypted)
    actual_minor = read_money(transaction, "amount_encrypted", data_key=data_key).amount_minor
    assert actual_minor == expected_minor
    assert transaction.currency == currency


@pytest.mark.parametrize(
    ("currency", "entered"),
    [
        ("XYZ", "100"),
        ("KRW", "100.1"),
        ("USD", "10.001"),
        ("JPY", "2.5"),
        ("VND", "3.0001"),
    ],
)
def test_unsupported_currency_or_fraction_is_rejected_without_writes(
    owner: Any,
    account: FinancialAccount,
    currency: str,
    entered: str,
) -> None:
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("transaction-new"),
        data={
            "occurred_at": "2026-08-15",
            "amount": entered,
            "currency": currency,
            "transaction_type": CanonicalTransaction.TransactionType.PURCHASE,
            "financial_account": str(account.pk),
            "merchant": "Synthetic currency test",
        },
    )

    assert response.status_code == 200
    assert CanonicalTransaction.objects.filter(user=owner).count() == 0
    form = response.context["form"]
    assert form.errors.get("currency") or form.errors.get("amount")
