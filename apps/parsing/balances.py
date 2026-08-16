"""Validate visible running balances to detect OCR digit errors.

Bank screenshots usually print the balance left after each row. When two
consecutive balances and the amount between them agree, every field in the row
gains confidence. When they disagree, one of the three was misread — but which
one is unknowable, so nothing is ever rewritten. The mismatch is reported and
the row goes to review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from apps.core.value_objects import CurrencyMismatchError, Money


class BalanceStatus(StrEnum):
    """The outcome of one balance check."""

    #: previous + signed amount == next
    VALID = "valid"
    #: The three values are all present and do not agree.
    INVALID = "invalid"
    #: Not enough balances were visible to check anything.
    UNAVAILABLE = "unavailable"


#: Confidence adjustments applied to a row's fields, never to its values.
CONFIDENCE_BONUS = 0.15
CONFIDENCE_PENALTY = -0.35


@dataclass(frozen=True, slots=True)
class BalanceValidation:
    """The result of comparing a balance chain against a signed amount."""

    status: BalanceStatus
    confidence_delta: float
    reason: str
    expected_next: Money | None = None
    observed_next: Money | None = None
    difference_minor: int | None = None

    @property
    def requires_review(self) -> bool:
        return self.status is BalanceStatus.INVALID

    @property
    def is_checked(self) -> bool:
        return self.status is not BalanceStatus.UNAVAILABLE


UNAVAILABLE = BalanceValidation(
    BalanceStatus.UNAVAILABLE,
    0.0,
    "no balance pair was visible",
)


@dataclass(frozen=True, slots=True)
class BalanceRow:
    """One parsed row's contribution to a running-balance chain.

    ``signed_amount_minor`` carries the economic sign: negative for money
    leaving the account. ``balance_after`` is the balance printed on the row.
    """

    signed_amount_minor: int | None
    balance_after: Money | None
    balance_before: Money | None = None


def validate_balance(
    *,
    previous_balance: Money | None,
    signed_amount_minor: int | None,
    next_balance: Money | None,
) -> BalanceValidation:
    """Check that ``previous + signed amount == next``.

    A missing balance or amount is not a failure: it simply leaves the row
    unchecked.
    """

    if previous_balance is None or next_balance is None or signed_amount_minor is None:
        return UNAVAILABLE
    try:
        expected = previous_balance + Money(signed_amount_minor, previous_balance.currency)
    except CurrencyMismatchError:
        return BalanceValidation(
            BalanceStatus.UNAVAILABLE,
            0.0,
            "balance and amount currencies differ",
        )
    if expected.resolved_currency != next_balance.resolved_currency:
        return BalanceValidation(
            BalanceStatus.UNAVAILABLE,
            0.0,
            "balance currencies differ across the chain",
        )
    difference = next_balance.amount_minor - expected.amount_minor
    if difference == 0:
        return BalanceValidation(
            BalanceStatus.VALID,
            CONFIDENCE_BONUS,
            "balance chain agrees with the parsed amount",
            expected,
            next_balance,
            0,
        )
    return BalanceValidation(
        BalanceStatus.INVALID,
        CONFIDENCE_PENALTY,
        f"balance chain is off by {difference} minor units",
        expected,
        next_balance,
        difference,
    )


def validate_balance_chain(rows: Sequence[BalanceRow]) -> tuple[BalanceValidation, ...]:
    """Validate a sequence of rows ordered oldest first.

    Each row is checked against the balance printed on the row before it, or
    against its own ``balance_before`` when the source shows one. The first row
    of a chain has nothing to compare against unless it carries a
    ``balance_before``.
    """

    validations: list[BalanceValidation] = []
    previous: Money | None = None
    for row in rows:
        anchor = row.balance_before if row.balance_before is not None else previous
        validations.append(
            validate_balance(
                previous_balance=anchor,
                signed_amount_minor=row.signed_amount_minor,
                next_balance=row.balance_after,
            )
        )
        if row.balance_after is not None:
            previous = row.balance_after
    return tuple(validations)


def apply_confidence(base_confidence: float, validation: BalanceValidation) -> float:
    """Adjust a field confidence by a validation result, clamped to [0, 1]."""

    return max(0.0, min(1.0, base_confidence + validation.confidence_delta))
