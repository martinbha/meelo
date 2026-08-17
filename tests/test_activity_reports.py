"""Account and card activity (#86, specification 25.3).

Two double counts to avoid, both specific. A debit-card purchase appears in the
card app and again in the bank app, and must land once. A card payment settles
purchases that were already counted when they were made, and must not be added
to spending — or hidden among other movement, since "you paid your card 380,000"
is a figure a person goes looking for.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.core.crypto import encrypt_model_field
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.instruments.models import PaymentInstrument
from apps.observations.review import accept_observation, merge_observations
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reports.activity import UNMAPPED_LABEL, account_activity, instrument_activity
from apps.reports.spending import month_bounds, monthly_spending
from apps.transactions.classification import is_settlement
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

_Type = CanonicalTransaction.TransactionType
AUGUST = month_bounds(2026, 8)


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="activity-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def checking(owner: Any) -> Any:
    return make_account(owner, name_encrypted="checking", name_blind_index="activity-checking")


def make_instrument(user: Any, account: Any, name: str, kind: str = "credit_card") -> Any:
    return PaymentInstrument.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"activity-{name}",
        instrument_type=kind,
        financial_account=account,
    )


def add(
    user: Any,
    account: Any,
    *,
    amount_minor: int,
    transaction_type: str = _Type.PURCHASE,
    instrument: Any = None,
    day: int = 15,
    currency: str = "KRW",
) -> CanonicalTransaction:
    return CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        payment_instrument=instrument,
        occurred_at=date(2026, 8, day),
        amount_encrypted=f"{amount_minor}:{currency}",
        currency=currency,
        transaction_type=transaction_type,
    )


def accounts(user: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {"start": AUGUST[0], "end": AUGUST[1]}
    values.update(overrides)
    return account_activity(user, **values)


def cards(user: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {"start": AUGUST[0], "end": AUGUST[1]}
    values.update(overrides)
    return instrument_activity(user, **values)


# ---------------------------------------------------------------------------
# Card payments are not card spending
# ---------------------------------------------------------------------------


def test_a_card_payment_is_reported_apart_from_spending(owner: Any, checking: Any) -> None:
    card = make_instrument(owner, checking, "visa")
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.PURCHASE, instrument=card)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT, day=25)

    report = accounts(owner)

    assert report.net_spending_minor == 380_000
    assert report.settlements_minor == 380_000
    # And it is not lumped in with transfers and withdrawals.
    assert report.movement_minor == 0


def test_a_loan_payment_is_a_balance_payment_too(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=250_000, transaction_type=_Type.LOAN_PAYMENT)

    report = accounts(owner)

    assert report.settlements_minor == 250_000
    assert report.net_spending_minor == 0
    assert is_settlement(_Type.LOAN_PAYMENT)


def test_transfers_and_withdrawals_stay_in_other_movement(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)
    add(owner, checking, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)

    report = accounts(owner)

    assert report.movement_minor == 600_000
    assert report.settlements_minor == 0


# ---------------------------------------------------------------------------
# One purchase, two screenshots
# ---------------------------------------------------------------------------


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("42900"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "이마트 성수점",
        "approval_code": "300142",
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def test_a_purchase_seen_in_two_apps_is_counted_once(
    owner: Any, checking: Any, data_key: bytes
) -> None:
    """The card app and the bank app both saw it; it is one purchase."""

    document = make_document(owner, file_sha256="b" * 64)
    run = make_ocr_run(owner, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(), parsed()),
        ),
        data_key=data_key,
        key_version=1,
    ).observations
    merge_observations(user=owner, winner_id=rows[0].pk, duplicate_ids=[rows[1].pk])
    accept_observation(
        rows[0].pk,
        user=owner,
        data_key=data_key,
        financial_account=checking,
        transaction_type=_Type.PURCHASE,
    )

    report = accounts(owner, data_key=data_key)

    assert report.net_spending_minor == 42_900
    assert report.transaction_count == 1


# ---------------------------------------------------------------------------
# Grouping and mapping
# ---------------------------------------------------------------------------


def test_activity_is_grouped_by_account_busiest_first(owner: Any, checking: Any) -> None:
    savings = make_account(owner, name_encrypted="savings", name_blind_index="activity-savings")
    add(owner, checking, amount_minor=10_000)
    add(owner, savings, amount_minor=40_000)

    report = accounts(owner)

    assert [(line.label, line.net_spending_minor) for line in report.lines] == [
        ("savings", 40_000),
        ("checking", 10_000),
    ]


def test_activity_is_grouped_by_card(owner: Any, checking: Any) -> None:
    visa = make_instrument(owner, checking, "visa")
    amex = make_instrument(owner, checking, "amex")
    add(owner, checking, amount_minor=10_000, instrument=visa)
    add(owner, checking, amount_minor=40_000, instrument=amex)

    report = cards(owner)

    assert [(line.label, line.net_spending_minor) for line in report.lines] == [
        ("amex", 40_000),
        ("visa", 10_000),
    ]


def test_activity_with_no_card_is_shown_and_sorted_last(owner: Any, checking: Any) -> None:
    """Unmapped activity is the thing a user needs to go and map."""

    add(owner, checking, amount_minor=90_000)
    add(owner, checking, amount_minor=10_000, instrument=make_instrument(owner, checking, "visa"))

    report = cards(owner)

    assert report.lines[-1].label == UNMAPPED_LABEL
    assert report.unmapped is not None
    assert report.unmapped.net_spending_minor == 90_000
    assert not report.lines[-1].is_mapped
    assert report.lines[0].is_mapped


def test_mapped_activity_alone_has_no_unmapped_line(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=10_000, instrument=make_instrument(owner, checking, "visa"))

    assert cards(owner).unmapped is None


# ---------------------------------------------------------------------------
# Reconciliation and filters
# ---------------------------------------------------------------------------


def test_account_totals_reconcile_with_the_month(owner: Any, checking: Any) -> None:
    savings = make_account(owner, name_encrypted="savings", name_blind_index="activity-savings")
    add(owner, checking, amount_minor=10_000)
    add(owner, savings, amount_minor=40_000)
    add(owner, checking, amount_minor=3_000, transaction_type=_Type.REFUND)
    add(owner, checking, amount_minor=3_000_000, transaction_type=_Type.INCOME)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)

    report = accounts(owner)
    month = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert report.net_spending_minor == month.net_spending_minor
    assert report.income_minor == month.income_minor
    # Settlements are part of the month's neutral figure, split out here.
    assert report.settlements_minor + report.movement_minor == month.neutral_minor
    assert report.transaction_count == month.transaction_count


def test_the_account_and_card_groupings_agree_on_the_totals(owner: Any, checking: Any) -> None:
    visa = make_instrument(owner, checking, "visa")
    add(owner, checking, amount_minor=10_000, instrument=visa)
    add(owner, checking, amount_minor=40_000)

    by_account = accounts(owner)
    by_card = cards(owner)

    assert by_account.net_spending_minor == by_card.net_spending_minor
    assert by_account.transaction_count == by_card.transaction_count


def test_an_account_filter_narrows_the_report(owner: Any, checking: Any) -> None:
    savings = make_account(owner, name_encrypted="savings", name_blind_index="activity-savings")
    add(owner, checking, amount_minor=10_000)
    add(owner, savings, amount_minor=40_000)

    assert accounts(owner, account_id=checking.pk).net_spending_minor == 10_000


def test_a_card_filter_narrows_the_report(owner: Any, checking: Any) -> None:
    visa = make_instrument(owner, checking, "visa")
    add(owner, checking, amount_minor=10_000, instrument=visa)
    add(owner, checking, amount_minor=40_000)

    assert cards(owner, instrument_id=visa.pk).net_spending_minor == 10_000


def test_another_users_activity_never_appears(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=10_000)
    stranger = make_user(email="activity-stranger@example.com")

    assert accounts(stranger).lines == ()


def test_encrypted_amounts_are_aggregated_in_the_application(
    owner: Any, checking: Any, data_key: bytes
) -> None:
    transaction = add(owner, checking, amount_minor=42_900)
    transaction.amount_encrypted = encrypt_model_field(
        transaction, "amount_encrypted", "42900:KRW", key=data_key, key_version=1
    )
    transaction.save(update_fields=["amount_encrypted"])

    assert accounts(owner, data_key=data_key).net_spending_minor == 42_900


def test_a_row_whose_currencies_disagree_is_refused(owner: Any, checking: Any) -> None:
    transaction = add(owner, checking, amount_minor=42_900, currency="KRW")
    CanonicalTransaction.objects.filter(pk=transaction.pk).update(amount_encrypted="42900:USD")

    with pytest.raises(ValueError, match="but its amount is encoded as"):
        accounts(owner)


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def test_the_account_page_shows_payments_apart_from_spending(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=42_900)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-accounts"), {"year": "2026", "month": "8"})

    assert response.status_code == 200
    assert response.context["report"].net_spending_minor == 42_900
    assert response.context["report"].settlements_minor == 380_000


def test_the_card_page_groups_by_card(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=10_000, instrument=make_instrument(owner, checking, "visa"))
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-cards"), {"year": "2026", "month": "8"})

    assert response.status_code == 200
    assert response.context["grouping"] == "instrument"
    assert "visa" in response.content.decode()


def test_the_pages_write_nothing_to_the_cache(owner: Any, checking: Any, monkeypatch: Any) -> None:
    add(owner, checking, amount_minor=42_900)
    cache.clear()
    writes: list[Any] = []
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: writes.append(args))
    client = Client()
    client.force_login(owner)

    for name in ("report-accounts", "report-cards"):
        response = client.get(reverse(name), {"year": "2026", "month": "8"})
        assert response.status_code == 200
        assert "no-store" in response.headers["Cache-Control"]

    assert writes == []


def test_another_users_account_cannot_be_used_as_a_filter(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=42_900)
    stranger = make_user(email="activity-filter-stranger@example.com")
    theirs = make_account(stranger, name_blind_index="activity-theirs")
    client = Client()
    client.force_login(owner)

    response = client.get(
        reverse("report-accounts"),
        {"year": "2026", "month": "8", "account": str(theirs.pk)},
    )

    assert response.context["selected_account"] is None
    assert response.context["report"].net_spending_minor == 42_900


def test_the_pages_require_a_login() -> None:
    for name in ("report-accounts", "report-cards"):
        response = Client().get(reverse(name))
        assert response.status_code == 302
        assert reverse("login") in response.headers["Location"]
