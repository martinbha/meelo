"""Income against spending, with everything else shown rather than hidden.

A month has four honest answers in it, not one: what came in, what went out for
good, what merely moved, and what nobody has decided about yet. Collapsing those
into a single "balance" is how a report ends up claiming a user earned half a
million won by moving money to their own savings account.

So this view keeps them apart, and it **shows the exclusions**. A total that
quietly leaves out transfers and card payments is indistinguishable, to the
person reading it, from a total that lost them to a bug. Naming the excluded
figure is what makes the omission checkable (specification 2.3, 25).

Two distinctions do most of the work:

- **A transfer is not income.** Money arriving in savings from checking is the
  same money, and counting it would invent a payday.
- **A settlement is not spending.** The purchases it pays for were counted when
  they were made.

And one that is easy to miss: **cash out of a machine is not cash spent.** A
withdrawal moves money from an account to a pocket. It becomes spending when it
is spent, if a screenshot of that ever arrives — so the two figures are reported
side by side and never added.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.transactions.classification import bucket_of, is_settlement
from apps.transactions.models import CanonicalTransaction

from .grouping import amount_in
from .spending import reportable_transactions


@dataclass(frozen=True, slots=True)
class OverviewFigure:
    """One figure on the overview, with the sentence that explains it."""

    label: str
    amount_minor: int
    transaction_count: int
    note: str = ""
    #: Whether this figure is part of net spending, or shown for context.
    counted: bool = True


@dataclass(frozen=True, slots=True)
class Overview:
    """A period split into income, spending, movement, and the undecided."""

    currency: str
    start: date
    end: date

    income_minor: int = 0
    income_count: int = 0

    card_expenses_minor: int = 0
    card_expenses_count: int = 0
    cash_expenses_minor: int = 0
    cash_expenses_count: int = 0
    refunds_minor: int = 0
    refunds_count: int = 0

    cash_withdrawals_minor: int = 0
    cash_withdrawals_count: int = 0
    settlements_minor: int = 0
    settlements_count: int = 0
    transfers_minor: int = 0
    transfers_count: int = 0

    unresolved_minor: int = 0
    unresolved_count: int = 0

    @property
    def gross_spending_minor(self) -> int:
        return self.card_expenses_minor + self.cash_expenses_minor

    @property
    def net_spending_minor(self) -> int:
        return self.gross_spending_minor - self.refunds_minor

    @property
    def net_position_minor(self) -> int:
        """What came in, less what went out for good."""

        return self.income_minor - self.net_spending_minor

    @property
    def excluded_minor(self) -> int:
        """Everything deliberately left out of the spending figure."""

        return self.cash_withdrawals_minor + self.settlements_minor + self.transfers_minor

    @property
    def has_unresolved(self) -> bool:
        return self.unresolved_count > 0

    def figures(self) -> tuple[OverviewFigure, ...]:
        """The whole period, in the order a person reads it."""

        return (
            OverviewFigure("Income", self.income_minor, self.income_count),
            OverviewFigure("Spent on cards", self.card_expenses_minor, self.card_expenses_count),
            OverviewFigure(
                "Spent in cash",
                self.cash_expenses_minor,
                self.cash_expenses_count,
                note="Purchases with no card attached.",
            ),
            OverviewFigure("Refunded", self.refunds_minor, self.refunds_count),
            OverviewFigure(
                "Cash withdrawn",
                self.cash_withdrawals_minor,
                self.cash_withdrawals_count,
                note=(
                    "Not spending. The money moved from an account to a pocket, "
                    "and counts when it is spent."
                ),
                counted=False,
            ),
            OverviewFigure(
                "Balance payments",
                self.settlements_minor,
                self.settlements_count,
                note=("Not spending. The purchases these settle were counted when they were made."),
                counted=False,
            ),
            OverviewFigure(
                "Transfers between your accounts",
                self.transfers_minor,
                self.transfers_count,
                note="Not income and not spending. The same money, somewhere else.",
                counted=False,
            ),
            OverviewFigure(
                "Not yet classified",
                self.unresolved_minor,
                self.unresolved_count,
                note=(
                    "Adjustments and transactions whose type is unknown. Counted "
                    "in neither total until you say what they were."
                ),
                counted=False,
            ),
        )


def _field_for(transaction: CanonicalTransaction) -> str:
    """Which pair of counters this transaction belongs to."""

    bucket = bucket_of(transaction.transaction_type)
    if bucket == "income":
        return "income"
    if bucket == "refund":
        return "refunds"
    if bucket == "unresolved":
        return "unresolved"
    if bucket == "spending":
        return "card_expenses" if transaction.payment_instrument_id is not None else "cash_expenses"
    if is_settlement(transaction.transaction_type):
        return "settlements"
    if transaction.transaction_type == CanonicalTransaction.TransactionType.CASH_WITHDRAWAL:
        return "cash_withdrawals"
    return "transfers"


_COUNTERS = (
    "income",
    "card_expenses",
    "cash_expenses",
    "refunds",
    "cash_withdrawals",
    "settlements",
    "transfers",
    "unresolved",
)


def summarize(
    transactions: Sequence[CanonicalTransaction],
    *,
    currency: str,
    start: date,
    end: date,
    data_key: bytes | None = None,
) -> Overview:
    """Split a set of transactions into the four honest answers."""

    running: dict[str, int] = dict.fromkeys(
        [f"{name}_minor" for name in _COUNTERS] + [f"{name}_count" for name in _COUNTERS], 0
    )
    for transaction in transactions:
        amount = amount_in(transaction, data_key=data_key, currency=currency)
        name = _field_for(transaction)
        running[f"{name}_minor"] += amount.amount_minor
        running[f"{name}_count"] += 1
    return Overview(currency=currency, start=start, end=end, **running)


def period_overview(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str = "KRW",
    data_key: bytes | None = None,
) -> Overview:
    """Income against spending for one user over one period."""

    resolved = currency.upper()
    rows = list(reportable_transactions(user, start=start, end=end).filter(currency=resolved))
    return summarize(rows, currency=resolved, start=start, end=end, data_key=data_key)
