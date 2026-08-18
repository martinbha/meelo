"""What each account actually holds, derived from the entries rather than read off a screen.

A screenshot's balance line is one bank's opinion at one moment, and it is the
first thing to go stale — it is wrong the instant anything else clears, it does
not exist at all on a card list, and two screenshots of the same account taken
minutes apart disagree. The ledger does not have that problem: it is every event
the user confirmed, and adding it up gives a figure that is consistent with the
reports by construction, because both read the same rows.

Two rules govern the arithmetic.

**Direction comes from the account, not the entry.** A debit is an increase to a
bank account and a decrease to a credit-card liability. Signing every entry the
same way would report a card balance that grows as it is paid off. So each
account's own ``normal_balance`` decides the sign, and the result reads the way a
person expects: an asset's balance is what is there, a liability's is what is
owed.

**Aggregation happens in the application, in integers.** The amounts are
encrypted, so the database cannot sum them (specification 25.4), and money is
never a float — every total here is integer minor units the whole way through.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from apps.core.value_objects import Currency, Money
from apps.transactions.models import CanonicalTransaction

from .models import LedgerAccount, LedgerEntry
from .posting import entry_amount

#: How each entry type moves an account, given what that account's normal
#: balance is. The whole of double-entry sign handling is this table.
NORMAL_BALANCE_SIGNS: dict[str, dict[str, int]] = {
    LedgerAccount.NormalBalance.DEBIT: {
        LedgerEntry.EntryType.DEBIT: 1,
        LedgerEntry.EntryType.CREDIT: -1,
    },
    LedgerAccount.NormalBalance.CREDIT: {
        LedgerEntry.EntryType.DEBIT: -1,
        LedgerEntry.EntryType.CREDIT: 1,
    },
}

#: Account types that add up to what the user owns and what they owe. Equity,
#: income, and expense accounts are the other side of those movements; counting
#: them into a net position would double every entry.
ASSET_TYPES: frozenset[str] = frozenset({LedgerAccount.AccountType.ASSET})
LIABILITY_TYPES: frozenset[str] = frozenset({LedgerAccount.AccountType.LIABILITY})


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """One ledger account's position in one currency."""

    account_id: UUID
    code: str
    account_type: str
    normal_balance: str
    #: The user-facing account this ledger account stands for, when it stands
    #: for one. Chart accounts such as an expense category do not.
    financial_account_id: UUID | None
    currency: str
    #: Positive means "more of whatever this account normally holds": money in
    #: an asset, debt in a liability.
    amount_minor: int
    entry_count: int

    @property
    def money(self) -> Money:
        return Money(self.amount_minor, Currency(self.currency))


@dataclass(frozen=True, slots=True)
class Position:
    """What one currency adds up to across every account."""

    currency: str
    assets_minor: int = 0
    liabilities_minor: int = 0

    @property
    def net_minor(self) -> int:
        """What is owned minus what is owed."""

        return self.assets_minor - self.liabilities_minor

    @property
    def net(self) -> Money:
        return Money(self.net_minor, Currency(self.currency))


def _entries(user: Any) -> Any:
    """Every entry that counts towards a balance, for one user.

    Only confirmed transactions. A voided one has been withdrawn, and its
    entries are cancelled by reversals — but relying on that cancellation would
    make the balance depend on the reversal having been written, and a balance
    should not be one missed step away from counting money the user said was
    never spent. Filtering on the status instead makes balances agree with the
    reports by construction, since both read confirmed history and nothing else.

    Both sides of the join are owner-filtered. The posting rules already refuse
    to pair one person's transaction with another's account, so this is the
    second lock on a door that is already shut — which is the right number of
    locks for the query that answers "how much money do I have".
    """

    return (
        LedgerEntry.objects.filter(
            account__user_id=user.pk,
            transaction__user_id=user.pk,
            transaction__status=CanonicalTransaction.Status.CONFIRMED,
        )
        .select_related("account", "transaction")
        .order_by("account__code", "created_at")
    )


def account_balances(user: Any, *, data_key: bytes | None = None) -> tuple[AccountBalance, ...]:
    """Every account's balance, one row per account and currency.

    An account holding two currencies gets two rows rather than one nonsense
    total. Nothing in this application converts between currencies, so adding
    them would invent an exchange rate.
    """

    totals: dict[tuple[UUID, str], int] = defaultdict(int)
    counts: dict[tuple[UUID, str], int] = defaultdict(int)
    accounts: dict[UUID, LedgerAccount] = {}

    for entry in _entries(user):
        amount = entry_amount(entry, data_key=data_key)
        account = entry.account
        accounts[account.pk] = account
        key = (account.pk, amount.resolved_currency.code)
        totals[key] += NORMAL_BALANCE_SIGNS[account.normal_balance][entry.entry_type] * (
            amount.amount_minor
        )
        counts[key] += 1

    return tuple(
        AccountBalance(
            account_id=account_id,
            code=accounts[account_id].code,
            account_type=accounts[account_id].account_type,
            normal_balance=accounts[account_id].normal_balance,
            financial_account_id=accounts[account_id].financial_account_id,
            currency=currency,
            amount_minor=amount_minor,
            entry_count=counts[(account_id, currency)],
        )
        for (account_id, currency), amount_minor in sorted(
            totals.items(), key=lambda item: (accounts[item[0][0]].code, item[0][1])
        )
    )


def positions(user: Any, *, data_key: bytes | None = None) -> dict[str, Position]:
    """Assets, liabilities, and the net of the two, per currency."""

    assets: dict[str, int] = defaultdict(int)
    liabilities: dict[str, int] = defaultdict(int)
    for balance in account_balances(user, data_key=data_key):
        if balance.account_type in ASSET_TYPES:
            assets[balance.currency] += balance.amount_minor
        elif balance.account_type in LIABILITY_TYPES:
            liabilities[balance.currency] += balance.amount_minor
    return {
        currency: Position(
            currency=currency,
            assets_minor=assets.get(currency, 0),
            liabilities_minor=liabilities.get(currency, 0),
        )
        for currency in sorted(set(assets) | set(liabilities))
    }


def financial_account_balances(
    user: Any, *, data_key: bytes | None = None
) -> dict[UUID, dict[str, int]]:
    """Balances keyed by the account a person recognises, not by chart code.

    A financial account may be represented by more than one ledger account —
    a card's liability and its settlement account, for instance — so the values
    are summed rather than assumed unique.
    """

    result: dict[UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for balance in account_balances(user, data_key=data_key):
        if balance.financial_account_id is None:
            continue
        result[balance.financial_account_id][balance.currency] += balance.amount_minor
    return {account_id: dict(currencies) for account_id, currencies in result.items()}
