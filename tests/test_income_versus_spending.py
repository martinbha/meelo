"""Income against spending, with exclusions shown (#87, specification 2.3, 25).

A month has four honest answers in it, not one: what came in, what went out for
good, what merely moved, and what nobody has decided about yet. Collapsing them
is how a report claims a user earned half a million won by moving money into
their own savings account.
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

from apps.core.key_management import provision_user_data_key
from apps.instruments.models import PaymentInstrument
from apps.reports import predicates
from apps.reports.overview import period_overview
from apps.reports.spending import month_bounds, monthly_spending, reportable_transactions
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
    user = make_user(email="overview-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def checking(owner: Any) -> Any:
    return make_account(owner, name_encrypted="checking", name_blind_index="overview-checking")


def make_card(user: Any, account: Any, name: str = "visa") -> Any:
    return PaymentInstrument.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"overview-{name}",
        instrument_type=PaymentInstrument.InstrumentType.CREDIT_CARD,
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


def overview(user: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {"start": AUGUST[0], "end": AUGUST[1]}
    values.update(overrides)
    return period_overview(user, **values)


# ---------------------------------------------------------------------------
# The two distinctions that do most of the work
# ---------------------------------------------------------------------------


def test_an_internal_transfer_is_not_income(owner: Any, checking: Any) -> None:
    """Counting it would invent a payday out of moving money to savings."""

    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    report = overview(owner)

    assert report.income_minor == 0
    assert report.transfers_minor == 500_000
    assert report.net_spending_minor == 0


def test_a_bank_transfer_is_not_income_either(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.BANK_TRANSFER)

    assert overview(owner).income_minor == 0


def test_a_settlement_is_not_spending(owner: Any, checking: Any) -> None:
    """The purchases it pays for were counted when they were made."""

    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)

    report = overview(owner)

    assert report.net_spending_minor == 0
    assert report.settlements_minor == 380_000


# ---------------------------------------------------------------------------
# Cash withdrawn is not cash spent
# ---------------------------------------------------------------------------


def test_a_withdrawal_and_a_cash_purchase_are_reported_apart(owner: Any, checking: Any) -> None:
    """One moved money to a pocket; the other spent it. Never the same figure."""

    add(owner, checking, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)
    add(owner, checking, amount_minor=12_000, transaction_type=_Type.PURCHASE)

    report = overview(owner)

    assert report.cash_withdrawals_minor == 100_000
    assert report.cash_expenses_minor == 12_000
    assert report.net_spending_minor == 12_000


def test_card_and_cash_spending_are_reported_apart(owner: Any, checking: Any) -> None:
    card = make_card(owner, checking)
    add(owner, checking, amount_minor=42_900, instrument=card)
    add(owner, checking, amount_minor=12_000)

    report = overview(owner)

    assert report.card_expenses_minor == 42_900
    assert report.cash_expenses_minor == 12_000
    assert report.gross_spending_minor == 54_900


# ---------------------------------------------------------------------------
# Nothing is hidden
# ---------------------------------------------------------------------------


def test_everything_excluded_from_spending_is_named(owner: Any, checking: Any) -> None:
    """A quiet omission is indistinguishable from a bug."""

    add(owner, checking, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    report = overview(owner)

    assert report.excluded_minor == 980_000
    excluded = [figure for figure in report.figures() if not figure.counted]
    assert sum(figure.amount_minor for figure in excluded) == 980_000
    # And each one says why it is excluded.
    assert all(figure.note for figure in excluded if figure.amount_minor)


def test_unknown_transactions_are_counted_apart_and_shown(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=5_000, transaction_type=_Type.ADJUSTMENT)
    add(owner, checking, amount_minor=7_000, transaction_type=_Type.UNKNOWN)

    report = overview(owner)

    assert report.unresolved_minor == 12_000
    assert report.unresolved_count == 2
    assert report.has_unresolved
    assert report.net_spending_minor == 0
    assert report.income_minor == 0


def test_every_figure_carries_a_count(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=10_000)
    add(owner, checking, amount_minor=20_000)

    figures = {figure.label: figure for figure in overview(owner).figures()}

    assert figures["Spent in cash"].transaction_count == 2


# ---------------------------------------------------------------------------
# A hand-calculated period
# ---------------------------------------------------------------------------


def test_a_hand_calculated_period_adds_up(owner: Any, checking: Any) -> None:
    """Worked out with a pen.

    Income                                       3,000,000
    Card spending          42,900 + 18,000  =       60,900
    Cash spending                                   12,000
    Gross spending                                  72,900
    Refund                                       -  20,000
    Net spending                                    52,900
    Difference (income less net spending)        2,947,100

    Excluded: withdrawal 100,000 + settlement 380,000
              + transfer 500,000              =    980,000
    """

    card = make_card(owner, checking)
    add(owner, checking, amount_minor=3_000_000, transaction_type=_Type.INCOME)
    add(owner, checking, amount_minor=42_900, instrument=card)
    add(owner, checking, amount_minor=18_000, instrument=card)
    add(owner, checking, amount_minor=12_000)
    add(owner, checking, amount_minor=20_000, transaction_type=_Type.REFUND, instrument=card)
    add(owner, checking, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    report = overview(owner)

    assert report.income_minor == 3_000_000
    assert report.card_expenses_minor == 60_900
    assert report.cash_expenses_minor == 12_000
    assert report.gross_spending_minor == 72_900
    assert report.refunds_minor == 20_000
    assert report.net_spending_minor == 52_900
    assert report.net_position_minor == 2_947_100
    assert report.excluded_minor == 980_000


def test_the_overview_agrees_with_the_monthly_totals(owner: Any, checking: Any) -> None:
    card = make_card(owner, checking)
    add(owner, checking, amount_minor=3_000_000, transaction_type=_Type.INCOME)
    add(owner, checking, amount_minor=42_900, instrument=card)
    add(owner, checking, amount_minor=12_000)
    add(owner, checking, amount_minor=20_000, transaction_type=_Type.REFUND)
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)
    add(owner, checking, amount_minor=5_000, transaction_type=_Type.ADJUSTMENT)

    report = overview(owner)
    month = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert report.net_spending_minor == month.net_spending_minor
    assert report.income_minor == month.income_minor
    assert report.excluded_minor == month.neutral_minor
    assert report.unresolved_minor == month.unresolved_minor


def test_another_users_period_is_empty(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=10_000)
    stranger = make_user(email="overview-stranger@example.com")

    report = overview(stranger)

    assert report.net_spending_minor == 0
    assert report.income_minor == 0


def test_a_row_whose_currencies_disagree_is_refused(owner: Any, checking: Any) -> None:
    transaction = add(owner, checking, amount_minor=42_900, currency="KRW")
    CanonicalTransaction.objects.filter(pk=transaction.pk).update(amount_encrypted="42900:USD")

    with pytest.raises(ValueError, match="but its amount is encoded as"):
        overview(owner)


# ---------------------------------------------------------------------------
# The predicates
# ---------------------------------------------------------------------------


def test_the_predicates_select_what_the_classification_says(owner: Any, checking: Any) -> None:
    """Python and SQL must agree; both read the same frozensets."""

    card = make_card(owner, checking)
    add(owner, checking, amount_minor=42_900, instrument=card)
    add(owner, checking, amount_minor=12_000)
    add(owner, checking, amount_minor=3_000_000, transaction_type=_Type.INCOME)
    add(owner, checking, amount_minor=20_000, transaction_type=_Type.REFUND)
    add(owner, checking, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    add(owner, checking, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)
    add(owner, checking, amount_minor=5_000, transaction_type=_Type.UNKNOWN)
    rows = reportable_transactions(owner, start=AUGUST[0], end=AUGUST[1])

    assert rows.filter(predicates.SPENDING).count() == 2
    assert rows.filter(predicates.CARD_EXPENSES).count() == 1
    assert rows.filter(predicates.CASH_EXPENSES).count() == 1
    assert rows.filter(predicates.INCOME).count() == 1
    assert rows.filter(predicates.REFUNDS).count() == 1
    assert rows.filter(predicates.SETTLEMENTS).count() == 1
    assert rows.filter(predicates.CASH_WITHDRAWALS).count() == 1
    assert rows.filter(predicates.OTHER_MOVEMENT).count() == 1
    assert rows.filter(predicates.UNRESOLVED).count() == 1
    assert rows.filter(predicates.EXCLUDED_FROM_SPENDING).count() == 2


def test_the_predicates_partition_every_reportable_row(owner: Any, checking: Any) -> None:
    """Nothing selected twice, nothing left out."""

    for kind in _Type.values:
        add(owner, checking, amount_minor=1_000, transaction_type=kind)
    rows = reportable_transactions(owner, start=AUGUST[0], end=AUGUST[1])
    partition = (
        predicates.SPENDING,
        predicates.REFUNDS,
        predicates.INCOME,
        predicates.NEUTRAL,
        predicates.UNRESOLVED,
    )

    counts = [rows.filter(part).count() for part in partition]

    assert sum(counts) == rows.count() == len(_Type.values)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_shows_the_exclusions(owner: Any, checking: Any) -> None:
    add(owner, checking, amount_minor=42_900)
    add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-overview"), {"year": "2026", "month": "8"})
    body = response.content.decode()

    assert response.status_code == 200
    assert response.context["overview"].excluded_minor == 500_000
    assert "Shown for context" in body
    assert "Transfers between your accounts" in body


def test_the_page_writes_nothing_to_the_cache(owner: Any, checking: Any, monkeypatch: Any) -> None:
    add(owner, checking, amount_minor=42_900)
    cache.clear()
    writes: list[Any] = []
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: writes.append(args))
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-overview"), {"year": "2026", "month": "8"})

    assert response.status_code == 200
    assert writes == []
    assert "no-store" in response.headers["Cache-Control"]


def test_the_page_requires_a_login() -> None:
    response = Client().get(reverse("report-overview"))

    assert response.status_code == 302
    assert reverse("login") in response.headers["Location"]


def test_the_breakdown_narrows_in_the_database(owner: Any, checking: Any) -> None:
    """Fetching a month's transfers only to discard them is what fetching costs."""

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.reports.breakdown import category_breakdown

    add(owner, checking, amount_minor=42_900)
    for _ in range(20):
        add(owner, checking, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    with CaptureQueriesContext(connection) as captured:
        report = category_breakdown(owner, start=AUGUST[0], end=AUGUST[1])

    assert report.transaction_count == 1
    # The transfers were excluded by the query rather than in Python.
    assert any("transaction_type" in query["sql"] for query in captured.captured_queries)
