from datetime import date
from decimal import Decimal

import pytest

from apps.core.value_objects import (
    Currency,
    CurrencyMismatchError,
    InvalidCurrencyError,
    InvalidDateError,
    InvalidMoneyError,
    Money,
    TransactionDate,
)


def test_currency_normalizes_codes_and_exposes_minor_unit_scale() -> None:
    assert str(Currency(" krw ")) == "KRW"
    assert Currency("KRW").decimal_places == 0
    assert Currency("USD").decimal_places == 2


def test_currency_rejects_non_three_letter_codes() -> None:
    with pytest.raises(InvalidCurrencyError):
        Currency("US")


def test_money_uses_integer_minor_units_and_decimal_conversion() -> None:
    krw = Money.from_decimal("42,900".replace(",", ""), "krw")
    usd = Money.from_decimal(Decimal("10.25"), "USD")

    assert krw.amount_minor == 42900
    assert usd.amount_minor == 1025
    assert usd.decimal_amount == Decimal("10.25")
    assert str(krw) == "42900 KRW"


def test_money_rounds_half_up_and_supports_arithmetic() -> None:
    value = Money.from_decimal("10.256", "USD")

    assert value.amount_minor == 1026
    assert value + Money(100, "USD") == Money(1126, "USD")
    assert value - Money(26, "USD") == Money(1000, "USD")
    assert -value == Money(-1026, "USD")


def test_money_rejects_mixed_currencies_and_non_integer_minor_units() -> None:
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") + Money(1, "KRW")
    with pytest.raises(InvalidMoneyError):
        Money(1.5, "USD")  # type: ignore[arg-type]
    with pytest.raises(InvalidMoneyError):
        Money.from_decimal("NaN", "USD")


def test_transaction_date_parses_and_orders_iso_dates() -> None:
    earlier = TransactionDate.parse("2026-08-05")
    later = TransactionDate.parse(date(2026, 8, 7))

    assert str(earlier) == "2026-08-05"
    assert earlier < later
    assert (later.year, later.month, later.day) == (2026, 8, 7)


def test_transaction_date_rejects_invalid_values() -> None:
    with pytest.raises(InvalidDateError):
        TransactionDate.parse("08/07/2026")
