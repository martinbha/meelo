"""Database predicates for each kind of transaction.

The classification in :mod:`apps.transactions.classification` answers "what is
this type" in Python. This module turns the same answers into ``Q`` objects, so a
report can narrow in the database instead of fetching a month and discarding most
of it. The two must agree, and they do because both read the same frozensets —
there is no second list of type names anywhere.

Reaching for a direction or an account instead of a type is the mistake these
exist to prevent. "Money that left the account" is not spending; it is spending,
a transfer, a card payment, and a cash withdrawal wearing the same hat.
"""

from __future__ import annotations

from django.db.models import Q

from apps.transactions.classification import (
    INCOME_TYPES,
    NEUTRAL_TYPES,
    REFUND_TYPES,
    SETTLEMENT_TYPES,
    SPENDING_TYPES,
    UNRESOLVED_TYPES,
)
from apps.transactions.models import CanonicalTransaction

_Type = CanonicalTransaction.TransactionType


def _types(names: frozenset[str]) -> Q:
    """A predicate over a set of transaction types, in a stable order.

    Sorted so the generated SQL is identical run to run, which matters when
    somebody is reading a query log to work out why a total moved.
    """

    return Q(transaction_type__in=sorted(names))


#: Money that left for something consumed.
SPENDING = _types(SPENDING_TYPES)
#: Money that came back from a purchase already counted.
REFUNDS = _types(REFUND_TYPES)
#: Money that arrived from outside every account the user owns.
INCOME = _types(INCOME_TYPES)
#: Movement: transfers, settlements, withdrawals, loan principal.
NEUTRAL = _types(NEUTRAL_TYPES)
#: The subset of movement that pays down a balance.
SETTLEMENTS = _types(SETTLEMENT_TYPES)
#: Movement that is not a settlement — transfers and withdrawals.
OTHER_MOVEMENT = NEUTRAL & ~SETTLEMENTS
#: Types nobody has decided about yet.
UNRESOLVED = _types(UNRESOLVED_TYPES)

#: Cash leaving an account for a pocket. Not spending: the money still exists,
#: and it will be counted when it is actually spent — if a screenshot of that
#: purchase ever arrives.
CASH_WITHDRAWALS = Q(transaction_type=_Type.CASH_WITHDRAWAL)

#: Spending with no card attached, which in practice means cash out of a pocket.
#: Shown apart from withdrawals so the two are never read as the same money.
CASH_EXPENSES = SPENDING & Q(payment_instrument__isnull=True)
#: Spending on a card.
CARD_EXPENSES = SPENDING & Q(payment_instrument__isnull=False)

#: Everything a spending total deliberately leaves out. Named so a report can
#: show the figure rather than merely omitting it: a user who cannot see what was
#: excluded has no way to tell exclusion from a bug.
EXCLUDED_FROM_SPENDING = NEUTRAL
