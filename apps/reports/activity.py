"""Activity by financial account and by payment instrument.

This report exists to answer "what happened on this card" without producing the
double count that makes the answer useless. Two dangers, both specific:

- **One purchase, two screenshots.** A debit-card purchase appears in the card
  app and again in the bank app. Reconciliation merges those into one
  observation, and reports read only :class:`CanonicalTransaction`, so a merged
  pair contributes once no matter how many screenshots it came from.
- **A card payment is not card spending.** The purchases it settles were already
  counted when they were made. It gets its own column rather than being folded
  into the spending figure or hidden among other movement — "you paid your card
  380,000" is a number a person looks for (specification 25.3).

Activity with no card attached is shown on its own line rather than dropped.
Unmapped activity is the thing a user needs to go and map.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.core.crypto import read_model_field
from apps.transactions.classification import bucket_of, is_settlement
from apps.transactions.models import CanonicalTransaction

from .grouping import group_rows, sorted_lines
from .spending import reportable_transactions

#: Shown for transactions that are not attached to any card.
UNMAPPED_LABEL = "No card"


@dataclass(frozen=True, slots=True)
class ActivityLine:
    """What happened on one account or one card over the period."""

    key: str
    label: str
    gross_spending_minor: int = 0
    refunds_minor: int = 0
    income_minor: int = 0
    #: Payments against a card or loan balance. Kept apart from spending on
    #: purpose: the money was already counted when it was spent.
    settlements_minor: int = 0
    #: Transfers and withdrawals — movement that is neither.
    movement_minor: int = 0
    unresolved_minor: int = 0
    transaction_count: int = 0

    @property
    def net_spending_minor(self) -> int:
        return self.gross_spending_minor - self.refunds_minor

    @property
    def is_mapped(self) -> bool:
        return bool(self.key)


@dataclass(frozen=True, slots=True)
class ActivityReport:
    """One line per account or card, plus the period they describe."""

    currency: str
    start: date
    end: date
    grouping: str
    lines: tuple[ActivityLine, ...]

    def _sum(self, attribute: str) -> int:
        return sum(getattr(line, attribute) for line in self.lines)

    @property
    def gross_spending_minor(self) -> int:
        return self._sum("gross_spending_minor")

    @property
    def refunds_minor(self) -> int:
        return self._sum("refunds_minor")

    @property
    def net_spending_minor(self) -> int:
        return self.gross_spending_minor - self.refunds_minor

    @property
    def income_minor(self) -> int:
        return self._sum("income_minor")

    @property
    def settlements_minor(self) -> int:
        return self._sum("settlements_minor")

    @property
    def movement_minor(self) -> int:
        return self._sum("movement_minor")

    @property
    def unresolved_minor(self) -> int:
        return self._sum("unresolved_minor")

    @property
    def transaction_count(self) -> int:
        return self._sum("transaction_count")

    @property
    def unmapped(self) -> ActivityLine | None:
        """Activity with no card attached, if any."""

        return next((line for line in self.lines if not line.key), None)


_FIELDS = (
    "gross_spending_minor",
    "refunds_minor",
    "income_minor",
    "settlements_minor",
    "movement_minor",
    "unresolved_minor",
    "transaction_count",
)


def _column_for(transaction: CanonicalTransaction) -> str:
    """Which column this transaction's amount belongs in."""

    bucket = bucket_of(transaction.transaction_type)
    if bucket == "spending":
        return "gross_spending_minor"
    if bucket == "refund":
        return "refunds_minor"
    if bucket == "income":
        return "income_minor"
    if bucket == "unresolved":
        return "unresolved_minor"
    return "settlements_minor" if is_settlement(transaction.transaction_type) else "movement_minor"


def _lines(grouped: dict[str, dict[str, Any]]) -> tuple[ActivityLine, ...]:
    return sorted_lines(
        ActivityLine(key=key, **{name: values[name] for name in ("label", *_FIELDS)})
        for key, values in grouped.items()
    )


def _accumulate(
    transactions: Sequence[CanonicalTransaction],
    *,
    data_key: bytes | None,
    key_of: Any,
    label_of: Any,
    currency: str,
) -> tuple[ActivityLine, ...]:
    return _lines(
        group_rows(
            transactions,
            data_key=data_key,
            currency=currency,
            key_of=key_of,
            label_of=label_of,
            column_for=_column_for,
            fields=_FIELDS,
        )
    )


def _scoped(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str,
    account_id: Any,
    instrument_id: Any,
) -> Any:
    queryset = reportable_transactions(user, start=start, end=end).filter(currency=currency.upper())
    if account_id is not None:
        queryset = queryset.filter(financial_account_id=account_id)
    if instrument_id is not None:
        queryset = queryset.filter(payment_instrument_id=instrument_id)
    return queryset


def account_activity(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str = "KRW",
    data_key: bytes | None = None,
    account_id: Any = None,
    instrument_id: Any = None,
) -> ActivityReport:
    """Activity grouped by financial account."""

    rows = list(
        _scoped(
            user,
            start=start,
            end=end,
            currency=currency,
            account_id=account_id,
            instrument_id=instrument_id,
        ).select_related("financial_account")
    )
    return ActivityReport(
        currency=currency.upper(),
        start=start,
        end=end,
        grouping="account",
        lines=_accumulate(
            rows,
            data_key=data_key,
            key_of=lambda item: str(item.financial_account_id),
            label_of=lambda item, key: read_model_field(
                item.financial_account, "name_encrypted", key=key
            ),
            currency=currency.upper(),
        ),
    )


def instrument_activity(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str = "KRW",
    data_key: bytes | None = None,
    account_id: Any = None,
    instrument_id: Any = None,
) -> ActivityReport:
    """Activity grouped by payment instrument, with unmapped rows shown."""

    rows = list(
        _scoped(
            user,
            start=start,
            end=end,
            currency=currency,
            account_id=account_id,
            instrument_id=instrument_id,
        ).select_related("payment_instrument")
    )
    return ActivityReport(
        currency=currency.upper(),
        start=start,
        end=end,
        grouping="instrument",
        lines=_accumulate(
            rows,
            data_key=data_key,
            key_of=lambda item: str(item.payment_instrument_id or ""),
            label_of=lambda item, key: (
                read_model_field(item.payment_instrument, "name_encrypted", key=key)
                if item.payment_instrument is not None
                else UNMAPPED_LABEL
            ),
            currency=currency.upper(),
        ),
    )
