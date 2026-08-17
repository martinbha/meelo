"""Which confirmed transactions count as spending, and which as income.

Reporting reads this module and nothing else, so a transaction type can only
start counting towards a total by being named here. A type nobody has placed
yet is neither spending nor income — silence means "not counted", never
"counted by default", because the expensive mistake in this domain is a total
that quietly grew a category nobody reviewed.

The one rule that matters most here is the internal transfer. Moving money from
a user's own checking account to their own savings account produces a debit row
and a credit row, and counting them would invent both an expense and an income
out of a single move that changed the user's net position by nothing
(specification 2.3, 7.4).
"""

from __future__ import annotations

from .models import CanonicalTransaction

_Type = CanonicalTransaction.TransactionType

#: Money that left for something consumed. These are the types a monthly
#: spending total is built from.
SPENDING_TYPES: frozenset[str] = frozenset(
    {
        _Type.PURCHASE,
        _Type.FEE,
        _Type.INTEREST,
    }
)

#: Money that arrived from outside every account the user owns.
INCOME_TYPES: frozenset[str] = frozenset({_Type.INCOME})

#: Money coming back from a purchase that was already counted. A refund is not
#: income: the user is less out of pocket than before, not better off than they
#: started, and counting it as income would inflate both totals at once
#: (specification 7.5, 17.4). It subtracts from the category it came from.
REFUND_TYPES: frozenset[str] = frozenset({_Type.REFUND})

#: Movements between accounts the user already owns. They change where the
#: money sits and nothing else, so they belong to neither total.
NEUTRAL_TYPES: frozenset[str] = frozenset({_Type.INTERNAL_TRANSFER})


def is_spending(transaction_type: str) -> bool:
    """Whether this type adds to a spending total."""

    return transaction_type in SPENDING_TYPES


def is_income(transaction_type: str) -> bool:
    """Whether this type adds to an income total."""

    return transaction_type in INCOME_TYPES


def is_spending_reduction(transaction_type: str) -> bool:
    """Whether this type subtracts from a spending total rather than adding.

    Kept apart from :func:`is_spending` so a caller cannot sum the two by
    accident: a refund and a purchase both touch the same category total, but
    with opposite signs.
    """

    return transaction_type in REFUND_TYPES


def is_neutral(transaction_type: str) -> bool:
    """Whether this type is a movement rather than a gain or a loss.

    Distinct from simply being unplaced: a neutral type has been considered and
    deliberately excluded, and reporting may say so rather than hiding it.
    """

    return transaction_type in NEUTRAL_TYPES
