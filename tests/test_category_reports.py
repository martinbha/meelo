"""Category and merchant reports (#85, specification 22.5, 25.3-25.4).

The database cannot add these up — `SUM()` over ciphertext is not a number — so
the arithmetic happens in the application process. Two things then have to hold:
the subtotals must reconcile with the month they claim to describe, and nothing
derived from them may be written to a cache, because a cached total is a
plaintext copy of somebody's finances living outside the encrypted store.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.categorization.models import Category
from apps.core.crypto import encrypt_model_field
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.reports.breakdown import (
    UNCATEGORIZED_LABEL,
    UNKNOWN_MERCHANT_LABEL,
    category_breakdown,
    merchant_breakdown,
    reconciles,
)
from apps.reports.spending import month_bounds, monthly_spending
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

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
    user = make_user(email="report-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner, name_blind_index="report-account")


def make_category(user: Any, name: str) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"report-{name}",
        category_type=Category.CategoryType.EXPENSE,
    )


def add(
    user: Any,
    account: Any,
    *,
    amount_minor: int,
    transaction_type: str = _Type.PURCHASE,
    category: Category | None = None,
    merchant: str = "",
    merchant_index: str = "",
    day: int = 15,
    month: int = 8,
    currency: str = "KRW",
) -> CanonicalTransaction:
    return CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, month, day),
        amount_encrypted=f"{amount_minor}:{currency}",
        currency=currency,
        transaction_type=transaction_type,
        category=category,
        merchant_encrypted=merchant,
        merchant_blind_index=merchant_index,
    )


def category_report(user: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {"start": AUGUST[0], "end": AUGUST[1]}
    values.update(overrides)
    return category_breakdown(user, **values)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_spending_is_grouped_by_category_largest_first(owner: Any, account: Any) -> None:
    food = make_category(owner, "food")
    travel = make_category(owner, "travel")
    add(owner, account, amount_minor=10_000, category=food)
    add(owner, account, amount_minor=5_000, category=food)
    add(owner, account, amount_minor=40_000, category=travel)

    report = category_report(owner)

    assert [(line.label, line.net_spending_minor) for line in report.lines] == [
        ("travel", 40_000),
        ("food", 15_000),
    ]
    assert report.lines[1].transaction_count == 2


def test_uncategorized_spending_is_shown_and_sorted_last(owner: Any, account: Any) -> None:
    """It is a call to action, not a category; hiding it mid-list buries it."""

    add(owner, account, amount_minor=90_000)
    add(owner, account, amount_minor=10_000, category=make_category(owner, "food"))

    report = category_report(owner)

    assert report.lines[-1].label == UNCATEGORIZED_LABEL
    assert report.unassigned is not None
    assert report.unassigned.net_spending_minor == 90_000


def test_refunds_reduce_the_category_they_came_from(owner: Any, account: Any) -> None:
    clothing = make_category(owner, "clothing")
    add(owner, account, amount_minor=200_000, category=clothing)
    add(owner, account, amount_minor=60_000, transaction_type=_Type.REFUND, category=clothing)

    line = category_report(owner).lines[0]

    assert line.gross_spending_minor == 200_000
    assert line.refunds_minor == 60_000
    assert line.net_spending_minor == 140_000
    assert line.has_refunds


def test_movement_and_income_never_reach_a_category_line(owner: Any, account: Any) -> None:
    food = make_category(owner, "food")
    add(owner, account, amount_minor=10_000, category=food)
    add(owner, account, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)
    add(owner, account, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    add(owner, account, amount_minor=3_000_000, transaction_type=_Type.INCOME)

    report = category_report(owner)

    assert len(report.lines) == 1
    assert report.net_spending_minor == 10_000


def test_merchants_are_grouped_on_their_blind_index(owner: Any, account: Any) -> None:
    """The name is encrypted; the index is what the database can group on."""

    add(owner, account, amount_minor=4_200, merchant="스타벅스", merchant_index="idx-sb")
    add(owner, account, amount_minor=5_800, merchant="스타벅스", merchant_index="idx-sb")
    add(owner, account, amount_minor=42_900, merchant="이마트", merchant_index="idx-em")

    report = merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1])

    assert [(line.label, line.net_spending_minor) for line in report.lines] == [
        ("이마트", 42_900),
        ("스타벅스", 10_000),
    ]


def test_rows_with_no_merchant_are_named_rather_than_dropped(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=4_200)

    report = merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1])

    assert report.lines[0].label == UNKNOWN_MERCHANT_LABEL


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_category_subtotals_reconcile_with_the_month(owner: Any, account: Any) -> None:
    food = make_category(owner, "food")
    travel = make_category(owner, "travel")
    add(owner, account, amount_minor=10_000, category=food)
    add(owner, account, amount_minor=40_000, category=travel)
    add(owner, account, amount_minor=7_000)
    add(owner, account, amount_minor=3_000, transaction_type=_Type.REFUND, category=food)
    add(owner, account, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    report = category_report(owner)
    month = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert reconciles(report, month)
    assert report.net_spending_minor == 54_000
    assert month.net_spending_minor == 54_000


def test_merchant_subtotals_reconcile_with_the_month(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=4_200, merchant="스타벅스", merchant_index="idx-sb")
    add(owner, account, amount_minor=42_900, merchant="이마트", merchant_index="idx-em")
    add(owner, account, amount_minor=1_000, transaction_type=_Type.REFUND, merchant_index="idx-em")

    report = merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1])
    month = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert reconciles(report, month)


def test_a_mismatch_is_reported_rather_than_raised(owner: Any, account: Any) -> None:
    """A disagreement has to be visible, not an error in front of a reader."""

    add(owner, account, amount_minor=10_000)
    report = category_report(owner)
    other_month = monthly_spending(owner, year=2026, month=7).totals("KRW")

    assert reconciles(report, other_month) is False


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_a_date_range_narrows_the_report(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=10_000, day=2)
    add(owner, account, amount_minor=40_000, day=20)

    narrowed = category_breakdown(owner, start=date(2026, 8, 1), end=date(2026, 8, 10))

    assert narrowed.net_spending_minor == 10_000


def test_a_category_filter_narrows_the_report(owner: Any, account: Any) -> None:
    food = make_category(owner, "food")
    add(owner, account, amount_minor=10_000, category=food)
    add(owner, account, amount_minor=40_000, category=make_category(owner, "travel"))

    filtered = category_report(owner, category_id=food.pk)

    assert filtered.net_spending_minor == 10_000
    assert len(filtered.lines) == 1


def test_a_currency_the_period_has_none_of_reports_nothing(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=10_000, currency="KRW")

    assert category_report(owner, currency="USD").lines == ()


def test_currencies_are_reported_apart(owner: Any, account: Any) -> None:
    food = make_category(owner, "food")
    add(owner, account, amount_minor=10_000, category=food, currency="KRW")
    add(owner, account, amount_minor=50, category=food, currency="USD")

    assert category_report(owner, currency="KRW").net_spending_minor == 10_000
    assert category_report(owner, currency="USD").net_spending_minor == 50


def test_another_users_spending_never_appears(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=10_000, category=make_category(owner, "food"))
    stranger = make_user(email="report-stranger@example.com")

    assert category_report(stranger).lines == ()


# ---------------------------------------------------------------------------
# Encrypted amounts
# ---------------------------------------------------------------------------


def test_encrypted_amounts_are_aggregated_in_the_application(
    owner: Any, account: Any, data_key: bytes
) -> None:
    food = make_category(owner, "food")
    for minor in (10_000, 5_000):
        transaction = add(owner, account, amount_minor=minor, category=food)
        transaction.amount_encrypted = encrypt_model_field(
            transaction, "amount_encrypted", f"{minor}:KRW", key=data_key, key_version=1
        )
        transaction.save(update_fields=["amount_encrypted"])

    report = category_report(owner, data_key=data_key)

    assert report.net_spending_minor == 15_000
    # Integer minor units throughout: no float has touched this number.
    assert isinstance(report.net_spending_minor, int)


def test_an_encrypted_merchant_is_decrypted_for_its_label(
    owner: Any, account: Any, data_key: bytes
) -> None:
    transaction = add(owner, account, amount_minor=4_200, merchant_index="idx-sb")
    transaction.merchant_encrypted = encrypt_model_field(
        transaction, "merchant_encrypted", "스타벅스", key=data_key, key_version=1
    )
    transaction.save(update_fields=["merchant_encrypted"])

    report = merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert report.lines[0].label == "스타벅스"


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def test_the_category_page_renders_its_lines(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900, category=make_category(owner, "food"))
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-categories"), {"year": 2026, "month": 8})

    assert response.status_code == 200
    assert response.context["breakdown"].net_spending_minor == 42_900
    assert response.context["reconciles"] is True


def test_the_merchant_page_groups_by_merchant(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=4_200, merchant="스타벅스", merchant_index="idx-sb")
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-merchants"), {"year": 2026, "month": 8})

    assert response.status_code == 200
    assert response.context["grouping"] == "merchant"
    assert "스타벅스" in response.content.decode()


def test_a_report_page_writes_nothing_to_the_cache(owner: Any, account: Any) -> None:
    """A cached total is a plaintext copy of somebody's finances."""

    add(owner, account, amount_minor=42_900, category=make_category(owner, "food"))
    cache.clear()
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-categories"), {"year": 2026, "month": 8})

    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("max-age=0, no-cache, no-store")


def test_a_nonsense_month_falls_back_rather_than_erroring(owner: Any, account: Any) -> None:
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-categories"), {"year": "abc", "month": "99"})

    assert response.status_code == 200


def test_another_users_category_cannot_be_used_as_a_filter(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900, category=make_category(owner, "food"))
    stranger = make_user(email="report-filter-stranger@example.com")
    theirs = make_category(stranger, "theirs")
    client = Client()
    client.force_login(owner)

    response = client.get(
        reverse("report-categories"),
        {"year": "2026", "month": "8", "category": str(theirs.pk)},
    )

    # The unknown filter is dropped rather than applied, so the owner still sees
    # their own month instead of an empty page implying they spent nothing.
    assert response.context["selected_category"] is None
    assert response.context["breakdown"].net_spending_minor == 42_900


def test_the_report_pages_require_a_login() -> None:
    for name in ("report-categories", "report-merchants"):
        response = Client().get(reverse(name))
        assert response.status_code == 302
        assert reverse("login") in response.headers["Location"]
