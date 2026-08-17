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

#: Money that moved without being earned or consumed. Every one of these is a
#: change of location: between the user's own accounts, out to a card issuer for
#: purchases already counted, from an account into a wallet, or against a loan
#: principal. Counting any of them would inflate a month by the size of a
#: movement the user did not make (specification 2.3, 25.1-25.2).
NEUTRAL_TYPES: frozenset[str] = frozenset(
    {
        _Type.INTERNAL_TRANSFER,
        _Type.BANK_TRANSFER,
        _Type.CREDIT_CARD_PAYMENT,
        _Type.CASH_WITHDRAWAL,
        _Type.LOAN_PAYMENT,
    }
)

#: The subset of neutral types that settle a card balance. Not a bucket of its
#: own — a settlement really is neutral — but reporting shows it apart from other
#: movement, because "you paid your card 380,000" is a figure a person looks for
#: and "you moved 380,000 around" is not (specification 25.3).
SETTLEMENT_TYPES: frozenset[str] = frozenset({_Type.CREDIT_CARD_PAYMENT, _Type.LOAN_PAYMENT})

#: Types nobody has decided about yet. Deliberately their own bucket rather than
#: folded into any total: an adjustment of unknown sign added to spending is a
#: wrong number, and one silently dropped is a number that does not add up.
#: Reporting shows these separately (specification 25, #87).
UNRESOLVED_TYPES: frozenset[str] = frozenset({_Type.ADJUSTMENT, _Type.UNKNOWN})


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


def is_settlement(transaction_type: str) -> bool:
    """Whether this type pays down a balance rather than buying anything."""

    return transaction_type in SETTLEMENT_TYPES


def is_unresolved(transaction_type: str) -> bool:
    """Whether this type is still waiting for someone to say what it was."""

    return transaction_type in UNRESOLVED_TYPES


#: Every transaction type, in exactly one bucket. A type missing from all of
#: them would vanish from every total without anything saying so, and a type in
#: two would be counted twice; the completeness test holds this shut.
BUCKETS: dict[str, frozenset[str]] = {
    "spending": SPENDING_TYPES,
    "income": INCOME_TYPES,
    "refund": REFUND_TYPES,
    "neutral": NEUTRAL_TYPES,
    "unresolved": UNRESOLVED_TYPES,
}


def bucket_of(transaction_type: str) -> str:
    """Which bucket a type belongs to, for reporting to add it up once."""

    for name, members in BUCKETS.items():
        if transaction_type in members:
            return name
    raise ValueError(f"Transaction type {transaction_type!r} is in no reporting bucket.")
