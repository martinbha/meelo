"""Where a month's spending went, by category and by merchant.

Every amount in this system is encrypted per user, so the database cannot add
them up. `SUM()` over a column of ciphertext is not a number. The arithmetic
therefore happens in the application process, over rows decrypted one at a time
and discarded — which is also why nothing here is ever written to a cache. A
cached category total is a plaintext copy of the user's finances sitting outside
the encrypted store, and an external cache is the one place it must never be
(specification 22.5, 25.3-25.4).

Grouping by merchant has the same shape of problem: the name is encrypted, so
rows are grouped by their **blind index**, which is queryable, and exactly one
representative name per group is decrypted for the label.

The invariant worth holding on to: **the lines add up to the total.** A
breakdown whose parts do not reconcile with the month it came from is worse than
no breakdown, because it looks like an answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.core.crypto import read_model_field
from apps.transactions.classification import bucket_of
from apps.transactions.models import CanonicalTransaction

from .grouping import group_rows, sorted_lines
from .spending import SpendingTotals, reportable_transactions

#: The two buckets that land on a category or a merchant. Income and pure
#: movement belong to the income-versus-spending view (#87), not here, and
#: mixing them in would break the reconciliation this module promises.
SPENDING_BUCKETS: frozenset[str] = frozenset({"spending", "refund"})

#: Shown for transactions nobody has categorised. Named rather than omitted:
#: money that fell out of every category is the first thing a user should see.
UNCATEGORIZED_LABEL = "Uncategorized"
#: Shown for rows whose merchant never parsed.
UNKNOWN_MERCHANT_LABEL = "Unknown merchant"


@dataclass(frozen=True, slots=True)
class BreakdownLine:
    """One category or merchant's share of the period."""

    key: str
    label: str
    gross_spending_minor: int = 0
    refunds_minor: int = 0
    transaction_count: int = 0

    @property
    def net_spending_minor(self) -> int:
        return self.gross_spending_minor - self.refunds_minor

    @property
    def has_refunds(self) -> bool:
        return self.refunds_minor > 0


@dataclass(frozen=True, slots=True)
class Breakdown:
    """Lines that add up to the period's net spending.

    Deliberately does not carry a :class:`~apps.reports.spending.SpendingTotals`.
    Only two of its five buckets apply here, and handing back one with
    ``income_minor`` sitting at zero would invite a caller to conclude there was
    no income — when in fact income was never in scope.
    """

    currency: str
    start: date
    end: date
    lines: tuple[BreakdownLine, ...]

    @property
    def gross_spending_minor(self) -> int:
        return sum(line.gross_spending_minor for line in self.lines)

    @property
    def refunds_minor(self) -> int:
        return sum(line.refunds_minor for line in self.lines)

    @property
    def net_spending_minor(self) -> int:
        return self.gross_spending_minor - self.refunds_minor

    @property
    def transaction_count(self) -> int:
        return sum(line.transaction_count for line in self.lines)

    @property
    def unassigned(self) -> BreakdownLine | None:
        """The line for rows with no category — or, by merchant, no merchant."""

        return next((line for line in self.lines if line.key == ""), None)


def _spending_rows(
    transactions: Iterable[CanonicalTransaction],
) -> list[CanonicalTransaction]:
    return [
        transaction
        for transaction in transactions
        if bucket_of(transaction.transaction_type) in SPENDING_BUCKETS
    ]


_FIELDS = ("gross_spending_minor", "refunds_minor", "transaction_count")


def _column_for(transaction: CanonicalTransaction) -> str:
    return (
        "refunds_minor"
        if bucket_of(transaction.transaction_type) == "refund"
        else "gross_spending_minor"
    )


def _lines(grouped: dict[str, dict[str, Any]]) -> tuple[BreakdownLine, ...]:
    return sorted_lines(
        BreakdownLine(key=key, **{name: values[name] for name in ("label", *_FIELDS)})
        for key, values in grouped.items()
    )


def _build(
    transactions: Sequence[CanonicalTransaction],
    *,
    data_key: bytes | None,
    key_of: Any,
    label_of: Any,
    currency: str,
    start: date,
    end: date,
) -> Breakdown:
    grouped = group_rows(
        transactions,
        data_key=data_key,
        currency=currency,
        key_of=key_of,
        label_of=label_of,
        column_for=_column_for,
        fields=_FIELDS,
    )
    return Breakdown(currency=currency, start=start, end=end, lines=_lines(grouped))


def reconciles(breakdown: Breakdown, totals: SpendingTotals) -> bool:
    """Whether a breakdown's lines add up to the month it claims to describe.

    Compared against a total computed independently by
    :func:`apps.reports.spending.monthly_spending`, because a breakdown checked
    only against its own sums checks nothing. Returned rather than raised: a
    disagreement has to be visible on the page, not an error in front of
    someone who only wanted to look at their month.
    """

    return breakdown.net_spending_minor == totals.net_spending_minor


def category_breakdown(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str = "KRW",
    data_key: bytes | None = None,
    category_id: Any = None,
) -> Breakdown:
    """Spending grouped by category over a date range."""

    queryset = reportable_transactions(user, start=start, end=end).select_related("category")
    # Filtered in the database: the currency column is queryable, so rows in
    # another currency are never fetched, let alone decrypted.
    queryset = queryset.filter(currency=currency.upper())
    if category_id is not None:
        queryset = queryset.filter(category_id=category_id)
    rows = _spending_rows(queryset)
    return _build(
        rows,
        data_key=data_key,
        key_of=lambda item: str(item.category_id) if item.category_id else "",
        label_of=lambda item, key: (
            read_model_field(item.category, "name_encrypted", key=key)
            if item.category is not None
            else UNCATEGORIZED_LABEL
        ),
        currency=currency.upper(),
        start=start,
        end=end,
    )


def merchant_breakdown(
    user: Any,
    *,
    start: date,
    end: date,
    currency: str = "KRW",
    data_key: bytes | None = None,
    category_id: Any = None,
) -> Breakdown:
    """Spending grouped by merchant over a date range.

    Grouped on the blind index, which is queryable and reveals nothing; the
    label comes from decrypting one representative row per group rather than
    every row in it.
    """

    queryset = reportable_transactions(user, start=start, end=end).filter(currency=currency.upper())
    if category_id is not None:
        queryset = queryset.filter(category_id=category_id)
    rows = _spending_rows(queryset)
    return _build(
        rows,
        data_key=data_key,
        key_of=lambda item: item.merchant_blind_index or "",
        label_of=lambda item, key: (
            read_model_field(item, "merchant_encrypted", key=key) or UNKNOWN_MERCHANT_LABEL
        ),
        currency=currency.upper(),
        start=start,
        end=end,
    )
