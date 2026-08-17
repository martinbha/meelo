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

from .amounts import transaction_amount
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
    """Lines that add up to the period's net spending, and the total they meet."""

    currency: str
    start: date
    end: date
    lines: tuple[BreakdownLine, ...]
    totals: SpendingTotals

    @property
    def net_spending_minor(self) -> int:
        return self.totals.net_spending_minor

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


def _lines(
    grouped: dict[str, dict[str, Any]],
) -> tuple[BreakdownLine, ...]:
    """Order lines by what costs most, with the uncategorised line last.

    Largest first is what a person reading a month wants. Uncategorised goes to
    the end regardless of size: it is a call to action rather than a category,
    and sorting it into the middle of the list hides it.
    """

    lines = [
        BreakdownLine(
            key=key,
            label=values["label"],
            gross_spending_minor=values["gross_spending_minor"],
            refunds_minor=values["refunds_minor"],
            transaction_count=values["transaction_count"],
        )
        for key, values in grouped.items()
    ]
    return tuple(
        sorted(lines, key=lambda line: (line.key == "", -line.net_spending_minor, line.label))
    )


def _group(
    transactions: Sequence[CanonicalTransaction],
    *,
    data_key: bytes | None,
    key_of: Any,
    label_of: Any,
    currency: str,
) -> dict[str, dict[str, Any]]:
    """Sum each group in one pass.

    One decryption per row, not two: reading an amount is the expensive part of
    a report, and computing the totals separately would double it (#90).
    """

    grouped: dict[str, dict[str, Any]] = {}
    for transaction in transactions:
        amount = transaction_amount(transaction, data_key=data_key)
        if amount.resolved_currency.code != currency:
            continue
        key = key_of(transaction)
        bucket = grouped.setdefault(
            key,
            {
                "label": label_of(transaction, data_key),
                "gross_spending_minor": 0,
                "refunds_minor": 0,
                "transaction_count": 0,
            },
        )
        field = (
            "refunds_minor"
            if bucket_of(transaction.transaction_type) == "refund"
            else "gross_spending_minor"
        )
        bucket[field] += amount.amount_minor
        bucket["transaction_count"] += 1
    return grouped


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
    grouped = _group(
        transactions, data_key=data_key, key_of=key_of, label_of=label_of, currency=currency
    )
    lines = _lines(grouped)
    # Built from the same sums the lines came from, so the two cannot disagree
    # about a row. ``reconciles`` then checks the arithmetic rather than the
    # bookkeeping, which is the part that actually goes wrong.
    totals = SpendingTotals(
        currency=currency,
        gross_spending_minor=sum(line.gross_spending_minor for line in lines),
        refunds_minor=sum(line.refunds_minor for line in lines),
        transaction_count=sum(line.transaction_count for line in lines),
    )
    return Breakdown(currency=currency, start=start, end=end, lines=lines, totals=totals)


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

    queryset = reportable_transactions(user, start=start, end=end)
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
