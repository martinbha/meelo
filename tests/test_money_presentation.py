from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.ledger.rules import post_transaction_by_type
from apps.reports.exports import export_row
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.money import read_money
from apps.transactions.services import create_manual_transaction
from tests.factories import make_account, make_ledger_accounts, make_user

pytestmark = pytest.mark.django_db

REPORT_NAMES = (
    "report-overview",
    "report-categories",
    "report-merchants",
    "report-accounts",
    "report-cards",
)


@pytest.fixture
def owner_and_key(tmp_path: Path, settings: Any) -> tuple[Any, bytes]:
    master = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_bytes(base64.urlsafe_b64encode(master))
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.FIELD_ENCRYPTION_REQUIRED = True
    owner = make_user(email="money-display@example.com")
    provision_user_data_key(user=owner, actor=owner, master_key=master)
    return owner, get_user_data_key(user=owner, actor=owner, master_key=master)


@pytest.mark.parametrize(
    ("currency", "minor", "formatted"),
    [
        ("KRW", 42_900, "₩42,900"),
        ("USD", 1_025, "$10.25"),
        ("USD", 1_234_567_890_234, "$12,345,678,902.34"),
    ],
)
@pytest.mark.parametrize("kind", ["purchase", "refund"])
def test_encrypted_posted_money_agrees_across_accounts_and_reports(
    owner_and_key: tuple[Any, bytes], currency: str, minor: int, formatted: str, kind: str
) -> None:
    owner, key = owner_and_key
    account = make_account(owner, currency=currency)
    transaction = create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 7),
        amount_minor=minor,
        currency=currency,
        transaction_type=kind,
        financial_account=account,
        merchant="Synthetic shop",
        data_key=key,
    )
    transaction = transition_transaction_status(
        transaction.pk, user=owner, status="confirmed", data_key=key
    )
    post_transaction_by_type(transaction, make_ledger_accounts(owner, account), data_key=key)
    client = Client()
    client.force_login(owner)

    for name in REPORT_NAMES:
        response = client.get(reverse(name), {"year": "2026", "month": "8", "currency": currency})
        assert response.status_code == 200, name
        html = response.content.decode()
        assert formatted in html, name
        assert f"<td>{minor}</td>" not in html, name
        assert f"<dd>{minor}</dd>" not in html, name

    for name, kwargs in (
        ("transaction-detail", {"pk": transaction.pk}),
        ("financial-account-list", {}),
        ("financial-account-detail", {"pk": account.pk}),
    ):
        response = client.get(reverse(name, kwargs=kwargs))
        assert response.status_code == 200, name
        assert formatted in response.content.decode(), name
        if name.startswith("financial-account"):
            signed = f"-{formatted}" if kind == "purchase" else formatted
            assert f"{signed} {currency}" in response.content.decode()

    transaction.refresh_from_db()
    assert read_money(transaction, "amount_encrypted", data_key=key).amount_minor == minor
    row = export_row(transaction, data_key=key)
    assert row["amount_minor"] == minor
    assert isinstance(row["amount_minor"], int)
    assert row["currency"] == currency


@pytest.mark.parametrize(("currency", "formatted"), [("KRW", "₩0"), ("USD", "$0.00")])
def test_empty_overview_formats_zero_in_the_requested_currency(
    owner_and_key: tuple[Any, bytes], currency: str, formatted: str
) -> None:
    client = Client()
    client.force_login(owner_and_key[0])
    response = client.get(reverse("report-overview"), {"currency": currency})

    assert response.status_code == 200
    assert f"<dt>Net spending</dt><dd>{formatted}</dd>" in response.content.decode()
    assert f"-{formatted}" not in response.content.decode()


@pytest.mark.parametrize("name", REPORT_NAMES)
def test_unsupported_report_currency_fails_before_rendering_money(
    owner_and_key: tuple[Any, bytes], name: str
) -> None:
    client = Client()
    client.force_login(owner_and_key[0])
    response = client.get(reverse(name), {"currency": "XYZ"})

    assert response.status_code == 400
    assert response["X-Error-Code"] == "INVALID_REQUEST"
