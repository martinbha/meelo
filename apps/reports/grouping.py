"""The grouping every report shares.

Two reports group encrypted transactions into lines — by category or merchant,
by account or card — and they differ only in what the columns mean. What they
must *not* differ in is the part that protects the numbers: the check that a
row's two currency records agree, and the rule that puts unassigned activity last
however large it is.

Both of those were written twice before this module existed, which is one copy
too many for a safety check. A drifting copy of the currency check would let one
report silently drop a row the other refuses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol

from apps.transactions.models import CanonicalTransaction

from .amounts import transaction_amount


class Line(Protocol):
    """The shape :func:`sorted_lines` needs from a report's line type.

    Read-only properties rather than attributes, so a frozen dataclass line
    satisfies it — which every line type here is, on purpose.
    """

    @property
    def key(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def net_spending_minor(self) -> int: ...


def group_rows(
    transactions: Sequence[CanonicalTransaction],
    *,
    data_key: bytes | None,
    currency: str,
    key_of: Callable[[CanonicalTransaction], str],
    label_of: Callable[[CanonicalTransaction, bytes | None], str],
    column_for: Callable[[CanonicalTransaction], str],
    fields: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Sum each group in one pass, one decryption per row.

    Reading an amount is the expensive part of a report, so the totals a caller
    needs are derived from these sums rather than computed in a second pass
    (#90).
    """

    grouped: dict[str, dict[str, Any]] = {}
    for transaction in transactions:
        amount = transaction_amount(transaction, data_key=data_key)
        if amount.resolved_currency.code != currency:
            # Callers filter on the queryable currency column, so reaching here
            # means the column and the encoded amount disagree. Skipping the row
            # would drop a real number out of a total with nothing said.
            raise ValueError(
                f"Transaction {transaction.pk} is recorded as {transaction.currency} "
                f"but its amount is encoded as {amount.resolved_currency.code}."
            )
        group = grouped.setdefault(
            key_of(transaction),
            {"label": label_of(transaction, data_key), **dict.fromkeys(fields, 0)},
        )
        group[column_for(transaction)] += amount.amount_minor
        group["transaction_count"] += 1
    return grouped


def sorted_lines[LineT: Line](lines: Iterable[LineT]) -> tuple[LineT, ...]:
    """Largest first, with the unassigned line last whatever its size.

    Largest first is what a person reading a month wants. The unassigned line —
    uncategorised spending, activity with no card — goes to the end regardless:
    it is a call to action rather than a category, and sorting it into the middle
    of the list is how it stays unassigned.
    """

    return tuple(
        sorted(lines, key=lambda line: (not line.key, -line.net_spending_minor, line.label))
    )
