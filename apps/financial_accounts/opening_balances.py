"""Where an account started, put into the ledger so the balance is right on day one.

An account opened with 2,400,000 KRW already in it has to say so, or every
balance derived from the ledger is 2,400,000 short until the user notices and
stops trusting the figure. The opening balance is therefore posted like anything
else — a balanced pair of entries, against an equity account, on a transaction of
its own.

Two things it is deliberately not.

**It is not income.** The money was not earned during any period this system
covers; it was already there. Posting it against equity rather than an income
account is what keeps it out of "what came in this month", and reporting
excludes the transaction type outright as a second line of defence.

**It is not editable.** Correcting an opening balance writes a further
adjustment for the difference and leaves the original posting alone. Rewriting
the first entry would make every balance ever computed from it retroactively
unexplainable — the figure would change and nothing would say why, or when, or
what it used to be.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction as db_transaction

from apps.core.audit import record_audit_event
from apps.core.crypto import read_model_field
from apps.core.errors import ForbiddenError, InvalidRequestError
from apps.core.value_objects import Currency, Money
from apps.ledger.chart import ensure_ledger_account_for, ensure_opening_balance_equity
from apps.ledger.models import LedgerAccount, LedgerEntry
from apps.ledger.posting import Posting, deserialize_money, post_balanced_transaction
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money

from .models import FinancialAccount

OPENING_BALANCE = CanonicalTransaction.TransactionType.OPENING_BALANCE


@dataclass(frozen=True, slots=True)
class OpeningBalancePosting:
    """One opening-balance posting, or the adjustment that corrected it."""

    transaction: CanonicalTransaction | None
    signed_minor: int
    is_adjustment: bool

    @property
    def posted(self) -> bool:
        return self.transaction is not None


def _opposite(entry_type: LedgerEntry.EntryType) -> LedgerEntry.EntryType:
    return (
        LedgerEntry.EntryType.CREDIT
        if entry_type == LedgerEntry.EntryType.DEBIT
        else LedgerEntry.EntryType.DEBIT
    )


def stored_opening_balance(
    account: FinancialAccount, *, data_key: bytes | None = None
) -> Money | None:
    """The opening balance recorded on the account, or ``None`` if there is none."""

    raw = read_model_field(account, "opening_balance_encrypted", key=data_key)
    if not raw:
        return None
    return deserialize_money(raw)


def posted_opening_balance_minor(
    account: FinancialAccount, *, data_key: bytes | None = None
) -> int:
    """The signed total already posted as opening balance for this account.

    Read from the ledger rather than from the column, because the ledger is what
    the balances are computed from. If the two ever disagree, the ledger is the
    one that is showing up in the user's totals.
    """

    ledger_account = LedgerAccount.objects.filter(financial_account=account).first()
    if ledger_account is None:
        return 0
    total = 0
    entries = LedgerEntry.objects.filter(
        account=ledger_account, transaction__transaction_type=OPENING_BALANCE
    ).select_related("transaction")
    for entry in entries:
        from apps.ledger.posting import entry_amount

        amount = entry_amount(entry, data_key=data_key).amount_minor
        # Signed in the account's own direction, so a liability's "positive"
        # means owed and an asset's means held.
        signed = amount if entry.entry_type == ledger_account.normal_balance else -amount
        total += signed
    return total


def _post_pair(
    *,
    account: FinancialAccount,
    user: Any,
    signed_minor: int,
    occurred_at: date,
    account_name: str,
    data_key: bytes | None,
    key_version: int,
) -> CanonicalTransaction:
    """Write one opening-balance transaction and its two ledger entries."""

    currency = Currency(account.currency)
    amount = Money(abs(signed_minor), currency)
    ledger_account = ensure_ledger_account_for(
        account, name=account_name, data_key=data_key, key_version=key_version
    )
    equity = ensure_opening_balance_equity(user, data_key=data_key, key_version=key_version)

    transaction = CanonicalTransaction(
        user=user,
        created_by=user,
        occurred_at=occurred_at,
        currency=currency.code,
        transaction_type=OPENING_BALANCE,
        financial_account=account,
        status=CanonicalTransaction.Status.DRAFT,
    )
    store_money(transaction, "amount_encrypted", amount, data_key=data_key, key_version=key_version)
    transaction.full_clean(validate_constraints=False)
    transaction.save()
    transaction = transition_transaction_status(
        transaction.pk,
        user=user,
        status=CanonicalTransaction.Status.CONFIRMED,
        data_key=data_key,
    )

    # Increasing the account in its own normal direction is what "an opening
    # balance of X" means; a negative one — an overdrawn account, a card already
    # in credit — simply flips both sides.
    increase = LedgerEntry.EntryType(ledger_account.normal_balance)
    account_entry = increase if signed_minor > 0 else _opposite(increase)
    post_balanced_transaction(
        transaction,
        [
            Posting(ledger_account, account_entry, amount),
            Posting(equity, _opposite(account_entry), amount),
        ],
        data_key=data_key,
        key_version=key_version,
    )
    return transaction


@db_transaction.atomic
def post_opening_balance(
    account: FinancialAccount,
    *,
    user: Any,
    occurred_at: date | None = None,
    account_name: str = "",
    data_key: bytes | None = None,
    key_version: int = 1,
) -> OpeningBalancePosting:
    """Post the account's recorded opening balance, once.

    A zero or absent opening balance posts nothing: an entry pair for zero is
    two rows saying nothing, and it would make every account look like it had
    been opened with a transaction.
    """

    if account.user_id != user.pk:
        raise ForbiddenError("This account belongs to another user.")

    recorded = stored_opening_balance(account, data_key=data_key)
    if recorded is None or recorded.amount_minor == 0:
        return OpeningBalancePosting(None, 0, is_adjustment=False)
    if recorded.resolved_currency != Currency(account.currency):
        raise InvalidRequestError(
            "The opening balance currency does not match the account currency."
        )
    already = posted_opening_balance_minor(account, data_key=data_key)
    if already != 0:
        raise InvalidRequestError(
            "This account already has an opening balance. Correct it with an adjustment."
        )

    transaction = _post_pair(
        account=account,
        user=user,
        signed_minor=recorded.amount_minor,
        occurred_at=occurred_at or account.created_at.date(),
        account_name=account_name,
        data_key=data_key,
        key_version=key_version,
    )
    record_audit_event(
        user=user,
        event_type="opening_balance_posted",
        obj=account,
        # The currency and the fact of it. Never the figure — specification 23.
        metadata={"currency": account.currency, "transaction_id": str(transaction.pk)},
    )
    return OpeningBalancePosting(transaction, recorded.amount_minor, is_adjustment=False)


@db_transaction.atomic
def correct_opening_balance(
    account: FinancialAccount,
    *,
    user: Any,
    corrected: Money,
    reason: str,
    occurred_at: date | None = None,
    account_name: str = "",
    data_key: bytes | None = None,
    key_version: int = 1,
) -> OpeningBalancePosting:
    """Adjust an opening balance by posting the difference.

    The original entries are never touched. Every balance the system has ever
    reported was computed from them, and silently changing one would leave those
    figures unexplainable — different today, with nothing recording what they
    used to be or when they changed. An adjustment gets to the same answer and
    keeps the history that led there.
    """

    if account.user_id != user.pk:
        raise ForbiddenError("This account belongs to another user.")
    if not reason.strip():
        raise InvalidRequestError("Correcting an opening balance requires a reason.")
    if corrected.resolved_currency != Currency(account.currency):
        raise InvalidRequestError("The corrected opening balance must use the account's currency.")

    already = posted_opening_balance_minor(account, data_key=data_key)
    difference = corrected.amount_minor - already

    store_money(
        account,
        "opening_balance_encrypted",
        corrected,
        data_key=data_key,
        key_version=key_version,
    )
    account.save(update_fields=["opening_balance_encrypted", "updated_at"])

    if difference == 0:
        return OpeningBalancePosting(None, 0, is_adjustment=True)

    transaction = _post_pair(
        account=account,
        user=user,
        signed_minor=difference,
        occurred_at=occurred_at or date.today(),
        account_name=account_name,
        data_key=data_key,
        key_version=key_version,
    )
    record_audit_event(
        user=user,
        event_type="opening_balance_adjusted",
        obj=account,
        metadata={
            "currency": account.currency,
            "transaction_id": str(transaction.pk),
            "reason": reason.strip()[:200],
        },
    )
    return OpeningBalancePosting(transaction, difference, is_adjustment=True)
