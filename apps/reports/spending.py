"""What a month cost, and what it only looked like it cost.

Spending is not "money that left the account". Most of what leaves a bank
account in a month is not spending at all: it moved to the user's own savings,
it paid off a card whose purchases were already counted when they were made, or
it came out of a machine and is still in their pocket. Adding those up produces
a figure that is roughly double the truth and looks entirely plausible, which is
why this module works from transaction *types* rather than from directions
(specification 2.3, 25.1-25.2).

Two rules carry most of the weight:

- **A credit-card purchase counts once, when it is bought.** The payment that
  settles the statement counts zero. Counting both would double every card
  purchase in the month.
- **A refund reduces the category it came from.** It is not income; the user is
  back where they started, not better off than they began.

Totals are kept per currency. Adding two currencies together is the kind of
mistake that produces a number nobody can trace, so it is not possible here.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db.models import QuerySet

from apps.core.ownership import owned_queryset
from apps.transactions.classification import bucket_of
from apps.transactions.models import CanonicalTransaction

from .amounts import transaction_amount

#: Statuses a report reads. A voided transaction is history the user withdrew,
#: and including it would report money they have said was never theirs to spend
#: (specification 25.2).
REPORTABLE_STATUSES: frozenset[str] = frozenset(
    {CanonicalTransaction.Status.DRAFT, CanonicalTransaction.Status.CONFIRMED}
)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """The first and last day of one month, inclusive at both ends."""

    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def reportable_transactions(
    user: Any, *, start: date | None = None, end: date | None = None
) -> QuerySet[CanonicalTransaction]:
    """Confirmed history for one user, scoped and status-filtered.

    Observations never appear here. A row a reviewer has not accepted, or has
    rejected, has no canonical transaction, so it cannot reach a total by any
    path — which is what keeps unreviewed screenshots out of the books.
    """

    queryset = owned_queryset(CanonicalTransaction, user).filter(status__in=REPORTABLE_STATUSES)
    if start is not None:
        queryset = queryset.filter(occurred_at__gte=start)
    if end is not None:
        queryset = queryset.filter(occurred_at__lte=end)
    return queryset


@dataclass(frozen=True, slots=True)
class SpendingTotals:
    """One currency's worth of a month, split by what each type means."""

    currency: str
    #: Purchases, fees, interest, and cash bought with cash.
    gross_spending_minor: int = 0
    #: Refunds, which subtract from spending rather than adding to income.
    refunds_minor: int = 0
    income_minor: int = 0
    #: Transfers, settlements, withdrawals — movement, not spending.
    neutral_minor: int = 0
    #: Adjustments and unknowns, shown rather than guessed at.
    unresolved_minor: int = 0
    transaction_count: int = 0

    @property
    def net_spending_minor(self) -> int:
        """What the month actually cost."""

        return self.gross_spending_minor - self.refunds_minor

    @property
    def net_position_minor(self) -> int:
        """Income less what was spent, ignoring pure movement."""

        return self.income_minor - self.net_spending_minor


@dataclass(frozen=True, slots=True)
class MonthlySpending:
    """A month's totals, one set per currency."""

    year: int
    month: int
    start: date
    end: date
    by_currency: Mapping[str, SpendingTotals] = field(default_factory=dict)

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_currency))

    def totals(self, currency: str) -> SpendingTotals:
        """One currency's totals, or an empty set rather than a KeyError.

        A month with no spending in a currency is a legitimate answer, and
        making the caller handle it as an error would push the same
        ``if`` into every report.
        """

        return self.by_currency.get(currency.upper(), SpendingTotals(currency.upper()))


_BUCKET_FIELDS: Mapping[str, str] = {
    "spending": "gross_spending_minor",
    "refund": "refunds_minor",
    "income": "income_minor",
    "neutral": "neutral_minor",
    "unresolved": "unresolved_minor",
}


def accumulate(
    transactions: Iterable[CanonicalTransaction], *, data_key: bytes | None = None
) -> dict[str, SpendingTotals]:
    """Add up transactions by currency and by what their type means.

    Pure arithmetic over rows the caller has already selected, so a report can
    be assembled from a queryset it built for its own reasons without this
    module deciding what belongs in it.
    """

    running: dict[str, dict[str, int]] = {}
    for transaction in transactions:
        amount = transaction_amount(transaction, data_key=data_key)
        currency = amount.resolved_currency.code
        if transaction.currency and transaction.currency.upper() != currency:
            # The column and the encoded amount disagree, so there is no honest
            # answer to which currency this row is in. Filing it under either
            # would put a real number in a total it does not belong to, and a
            # later query filtering on the column would disagree with this one.
            raise ValueError(
                f"Transaction {transaction.pk} is recorded as {transaction.currency} "
                f"but its amount is encoded as {currency}."
            )
        totals = running.setdefault(
            currency, dict.fromkeys([*_BUCKET_FIELDS.values(), "transaction_count"], 0)
        )
        totals[_BUCKET_FIELDS[bucket_of(transaction.transaction_type)]] += amount.amount_minor
        totals["transaction_count"] += 1
    return {
        currency: SpendingTotals(currency=currency, **values)
        for currency, values in running.items()
    }


def monthly_spending(
    user: Any, *, year: int, month: int, data_key: bytes | None = None
) -> MonthlySpending:
    """What one month cost this user, per currency."""

    start, end = month_bounds(year, month)
    return MonthlySpending(
        year=year,
        month=month,
        start=start,
        end=end,
        by_currency=accumulate(
            reportable_transactions(user, start=start, end=end), data_key=data_key
        ),
    )
