"""What a month cost (#84, specification 2.3, 25.1-25.2).

Most of what leaves a bank account in a month is not spending. It moved to
savings, it paid off a card whose purchases were already counted, or it came out
of a machine and is still in a pocket. Adding those up gives a figure roughly
double the truth that looks entirely plausible, so these tests work from
hand-calculated months rather than from anything the code produced.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest

from apps.core.crypto import encrypt_model_field
from apps.reports.amounts import is_encrypted, transaction_amount
from apps.reports.spending import (
    REPORTABLE_STATUSES,
    accumulate,
    month_bounds,
    monthly_spending,
    reportable_transactions,
)
from apps.transactions.classification import (
    BUCKETS,
    bucket_of,
    is_income,
    is_neutral,
    is_spending,
    is_spending_reduction,
    is_unresolved,
)
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

KEY = os.urandom(32)
_Type = CanonicalTransaction.TransactionType


@pytest.fixture
def owner() -> Any:
    return make_user(email="spending-owner@example.com")


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner, name_blind_index="spending-account")


def add(
    user: Any,
    account: Any,
    *,
    amount_minor: int,
    transaction_type: str,
    day: int = 15,
    month: int = 8,
    currency: str = "KRW",
    status: str = CanonicalTransaction.Status.CONFIRMED,
) -> CanonicalTransaction:
    return CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, month, day),
        amount_encrypted=f"{amount_minor}:{currency}",
        currency=currency,
        transaction_type=transaction_type,
        status=status,
    )


# ---------------------------------------------------------------------------
# The buckets
# ---------------------------------------------------------------------------


def test_every_transaction_type_is_in_exactly_one_bucket() -> None:
    """A type in no bucket vanishes from every total; one in two is counted twice."""

    placed: list[str] = [name for members in BUCKETS.values() for name in members]

    assert sorted(placed) == sorted(_Type.values)
    assert len(placed) == len(set(placed))


def test_an_unknown_type_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError):
        bucket_of("teleportation")


@pytest.mark.parametrize(
    ("transaction_type", "bucket"),
    [
        (_Type.PURCHASE, "spending"),
        (_Type.FEE, "spending"),
        (_Type.INTEREST, "spending"),
        (_Type.INCOME, "income"),
        (_Type.REFUND, "refund"),
        (_Type.INTERNAL_TRANSFER, "neutral"),
        (_Type.BANK_TRANSFER, "neutral"),
        (_Type.CREDIT_CARD_PAYMENT, "neutral"),
        (_Type.CASH_WITHDRAWAL, "neutral"),
        (_Type.LOAN_PAYMENT, "neutral"),
        (_Type.ADJUSTMENT, "unresolved"),
        (_Type.UNKNOWN, "unresolved"),
    ],
)
def test_each_type_lands_where_the_specification_puts_it(
    transaction_type: str, bucket: str
) -> None:
    assert bucket_of(transaction_type) == bucket


def test_the_predicates_agree_with_the_buckets() -> None:
    assert is_spending(_Type.PURCHASE)
    assert is_income(_Type.INCOME)
    assert is_spending_reduction(_Type.REFUND)
    assert is_neutral(_Type.CASH_WITHDRAWAL)
    assert is_unresolved(_Type.UNKNOWN)
    # A refund touches spending with the opposite sign, so it must not also
    # answer to is_spending: a caller summing both would report a larger total.
    assert not is_spending(_Type.REFUND)


# ---------------------------------------------------------------------------
# A hand-calculated month
# ---------------------------------------------------------------------------


def test_a_hand_calculated_month_adds_up(owner: Any, account: Any) -> None:
    """Worked out with a pen first, then asserted.

    Purchases       42,900 + 18,000 + 200,000 = 260,900
    Fee                                             1,500
    Interest                                        3,200
    Gross spending                                265,600
    Refund                                       - 60,000
    Net spending                                  205,600

    Income                                      3,000,000
    Neutral (transfer 500,000, settlement 380,000,
             withdrawal 100,000)                  980,000

    Ten rows in total: 3 purchases, fee, interest, refund, income, transfer,
    settlement, withdrawal.
    """

    for minor in (42_900, 18_000, 200_000):
        add(owner, account, amount_minor=minor, transaction_type=_Type.PURCHASE)
    add(owner, account, amount_minor=1_500, transaction_type=_Type.FEE)
    add(owner, account, amount_minor=3_200, transaction_type=_Type.INTEREST)
    add(owner, account, amount_minor=60_000, transaction_type=_Type.REFUND)
    add(owner, account, amount_minor=3_000_000, transaction_type=_Type.INCOME)
    add(owner, account, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)
    add(owner, account, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT)
    add(owner, account, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.gross_spending_minor == 265_600
    assert totals.refunds_minor == 60_000
    assert totals.net_spending_minor == 205_600
    assert totals.income_minor == 3_000_000
    assert totals.neutral_minor == 980_000
    assert totals.net_position_minor == 2_794_400
    assert totals.transaction_count == 10


def test_a_card_purchase_counts_once_and_its_settlement_counts_zero(
    owner: Any, account: Any
) -> None:
    """The money the settlement moves was already counted when it was spent."""

    add(owner, account, amount_minor=380_000, transaction_type=_Type.PURCHASE, day=3)
    add(owner, account, amount_minor=380_000, transaction_type=_Type.CREDIT_CARD_PAYMENT, day=25)

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.net_spending_minor == 380_000
    assert totals.neutral_minor == 380_000


def test_a_pure_cash_withdrawal_is_not_spending(owner: Any, account: Any) -> None:
    """The money is in a pocket, not gone."""

    add(owner, account, amount_minor=100_000, transaction_type=_Type.CASH_WITHDRAWAL)

    assert monthly_spending(owner, year=2026, month=8).totals("KRW").net_spending_minor == 0


def test_cash_spent_is_spending(owner: Any, account: Any) -> None:
    """A purchase is a purchase whether or not a card was involved."""

    add(owner, account, amount_minor=12_000, transaction_type=_Type.PURCHASE)

    assert monthly_spending(owner, year=2026, month=8).totals("KRW").net_spending_minor == 12_000


def test_savings_movement_is_excluded(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=500_000, transaction_type=_Type.BANK_TRANSFER)
    add(owner, account, amount_minor=500_000, transaction_type=_Type.INTERNAL_TRANSFER)

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.net_spending_minor == 0
    assert totals.income_minor == 0
    assert totals.neutral_minor == 1_000_000


def test_a_refund_reduces_spending_without_becoming_income(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=200_000, transaction_type=_Type.PURCHASE)
    add(owner, account, amount_minor=200_000, transaction_type=_Type.REFUND)

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.gross_spending_minor == 200_000
    assert totals.net_spending_minor == 0
    assert totals.income_minor == 0


def test_adjustments_are_counted_apart_rather_than_guessed_at(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=5_000, transaction_type=_Type.ADJUSTMENT)
    add(owner, account, amount_minor=7_000, transaction_type=_Type.UNKNOWN)

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.unresolved_minor == 12_000
    assert totals.net_spending_minor == 0
    assert totals.income_minor == 0


# ---------------------------------------------------------------------------
# What is left out
# ---------------------------------------------------------------------------


def test_a_voided_transaction_leaves_the_books(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)
    add(
        owner,
        account,
        amount_minor=99_000,
        transaction_type=_Type.PURCHASE,
        status=CanonicalTransaction.Status.VOIDED,
    )

    assert monthly_spending(owner, year=2026, month=8).totals("KRW").net_spending_minor == 42_900
    assert CanonicalTransaction.Status.VOIDED not in REPORTABLE_STATUSES


def test_an_accepted_but_unposted_transaction_still_counts(owner: Any, account: Any) -> None:
    """A person accepted it; the ledger posting is a separate step."""

    add(
        owner,
        account,
        amount_minor=42_900,
        transaction_type=_Type.PURCHASE,
        status=CanonicalTransaction.Status.DRAFT,
    )

    assert monthly_spending(owner, year=2026, month=8).totals("KRW").net_spending_minor == 42_900


def test_another_months_transactions_do_not_leak_in(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE, month=8, day=1)
    add(owner, account, amount_minor=99_000, transaction_type=_Type.PURCHASE, month=7, day=31)
    add(owner, account, amount_minor=77_000, transaction_type=_Type.PURCHASE, month=9, day=1)

    assert monthly_spending(owner, year=2026, month=8).totals("KRW").net_spending_minor == 42_900


def test_the_last_day_of_the_month_is_included(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=1_000, transaction_type=_Type.PURCHASE, month=2, day=28)

    assert monthly_spending(owner, year=2026, month=2).totals("KRW").net_spending_minor == 1_000
    assert month_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds(2028, 2)[1] == date(2028, 2, 29)


def test_an_impossible_month_is_refused() -> None:
    with pytest.raises(ValueError):
        month_bounds(2026, 13)


def test_another_users_spending_is_never_visible(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)
    stranger = make_user(email="spending-stranger@example.com")

    totals = monthly_spending(stranger, year=2026, month=8).totals("KRW")

    assert totals.net_spending_minor == 0
    assert totals.transaction_count == 0
    assert reportable_transactions(stranger).count() == 0


def test_a_currency_with_no_activity_answers_zero_rather_than_raising(
    owner: Any, account: Any
) -> None:
    add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)

    assert monthly_spending(owner, year=2026, month=8).totals("USD").net_spending_minor == 0


def test_currencies_are_never_added_together(owner: Any, account: Any) -> None:
    """A total mixing two currencies is a number nobody can trace."""

    add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE, currency="KRW")
    add(owner, account, amount_minor=50, transaction_type=_Type.PURCHASE, currency="USD")

    report = monthly_spending(owner, year=2026, month=8)

    assert report.currencies == ("KRW", "USD")
    assert report.totals("KRW").net_spending_minor == 42_900
    assert report.totals("USD").net_spending_minor == 50


# ---------------------------------------------------------------------------
# Reading amounts
# ---------------------------------------------------------------------------


def test_an_encrypted_amount_is_read_with_the_key(owner: Any, account: Any) -> None:
    transaction = add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)
    transaction.amount_encrypted = encrypt_model_field(
        transaction, "amount_encrypted", "42900:KRW", key=KEY, key_version=1
    )
    transaction.save(update_fields=["amount_encrypted"])

    assert is_encrypted(transaction.amount_encrypted)
    assert transaction_amount(transaction, data_key=KEY).amount_minor == 42_900
    assert accumulate([transaction], data_key=KEY)["KRW"].net_spending_minor == 42_900


def test_an_encrypted_amount_without_a_key_raises_rather_than_counting_zero(
    owner: Any, account: Any
) -> None:
    """A silently skipped row shrinks a month in the direction nobody checks."""

    transaction = add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)
    transaction.amount_encrypted = encrypt_model_field(
        transaction, "amount_encrypted", "42900:KRW", key=KEY, key_version=1
    )

    with pytest.raises(ValueError):
        transaction_amount(transaction)


def test_a_row_whose_currencies_disagree_is_refused(owner: Any, account: Any) -> None:
    """Filing it under either would put a real number in the wrong total."""

    transaction = add(owner, account, amount_minor=42_900, transaction_type=_Type.PURCHASE)
    CanonicalTransaction.objects.filter(pk=transaction.pk).update(currency="USD")

    with pytest.raises(ValueError, match="but its amount is encoded as"):
        monthly_spending(owner, year=2026, month=8)


def test_every_bucket_maps_to_a_field_on_the_totals() -> None:
    """A bucket with no field would fail at runtime on the first month using it."""

    from dataclasses import fields

    from apps.reports.spending import _BUCKET_FIELDS, SpendingTotals

    assert set(_BUCKET_FIELDS) == set(BUCKETS)
    available = {item.name for item in fields(SpendingTotals)}
    assert set(_BUCKET_FIELDS.values()) <= available
