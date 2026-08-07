from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import ClassVar, Self


class InvalidCurrencyError(ValueError):
    pass


class CurrencyMismatchError(ValueError):
    pass


class InvalidMoneyError(ValueError):
    pass


class InvalidDateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Currency:
    code: str

    _CODE_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")
    _ZERO_DECIMAL: ClassVar[frozenset[str]] = frozenset({"JPY", "KRW"})

    def __post_init__(self) -> None:
        normalized = self.code.strip().upper()
        if not self._CODE_PATTERN.fullmatch(normalized):
            raise InvalidCurrencyError("Currency must be a three-letter ISO-style code.")
        object.__setattr__(self, "code", normalized)

    @property
    def decimal_places(self) -> int:
        return 0 if self.code in self._ZERO_DECIMAL else 2

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class Money:
    """A currency amount represented as integer minor units."""

    amount_minor: int
    currency: Currency | str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise InvalidMoneyError("Money amounts must be integer minor units.")
        if isinstance(self.currency, str):
            object.__setattr__(self, "currency", Currency(self.currency))

    @classmethod
    def from_decimal(cls, value: Decimal | int | str, currency: Currency | str) -> Self:
        resolved_currency = currency if isinstance(currency, Currency) else Currency(currency)
        try:
            decimal_value = Decimal(str(value))
            if not decimal_value.is_finite():
                raise InvalidMoneyError("Money amount must be a finite decimal value.")
            quantum = Decimal(1).scaleb(-resolved_currency.decimal_places)
            rounded = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
            amount_minor = int(rounded.scaleb(resolved_currency.decimal_places))
        except (InvalidOperation, ValueError) as exc:
            raise InvalidMoneyError("Money amount must be a valid decimal value.") from exc
        return cls(amount_minor, resolved_currency)

    @property
    def resolved_currency(self) -> Currency:
        return self.currency if isinstance(self.currency, Currency) else Currency(self.currency)

    @property
    def decimal_amount(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.resolved_currency.decimal_places)

    def _assert_same_currency(self, other: Money) -> None:
        if self.resolved_currency != other.resolved_currency:
            raise CurrencyMismatchError("Money values must use the same currency.")

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount_minor), self.currency)

    def is_zero(self) -> bool:
        return self.amount_minor == 0

    def __str__(self) -> str:
        currency = self.resolved_currency
        return f"{self.decimal_amount:.{currency.decimal_places}f} {currency}"


@dataclass(frozen=True, order=True, slots=True)
class TransactionDate:
    value: date

    @classmethod
    def parse(cls, value: str | date) -> Self:
        if isinstance(value, date):
            return cls(value)
        try:
            return cls(date.fromisoformat(value.strip()))
        except (TypeError, ValueError) as exc:
            raise InvalidDateError(
                "Transaction dates must use ISO-8601 YYYY-MM-DD format."
            ) from exc

    @property
    def year(self) -> int:
        return self.value.year

    @property
    def month(self) -> int:
        return self.value.month

    @property
    def day(self) -> int:
        return self.value.day

    def __str__(self) -> str:
        return self.value.isoformat()
