from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction as db_transaction

from apps.core.encrypted_fields import require_encryption_key
from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Currency, Money
from apps.transactions.models import CanonicalTransaction

from .models import LedgerAccount, LedgerEntry


@dataclass(frozen=True, slots=True)
class Posting:
    account: LedgerAccount
    entry_type: LedgerEntry.EntryType
    amount: Money


def serialize_money(amount: Money) -> str:
    return f"{amount.amount_minor}:{amount.resolved_currency.code}"


def deserialize_money(value: str) -> Money:
    try:
        amount_minor, currency = value.split(":", maxsplit=1)
        return Money(int(amount_minor), currency)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(
            "Transaction amounts must be encoded as minor_units:CURRENCY."
        ) from exc


def post_balanced_transaction(
    canonical_transaction: CanonicalTransaction,
    postings: Sequence[Posting],
    *,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> list[LedgerEntry]:
    """Atomically post a confirmed transaction when debit and credit totals balance.

    Entry amounts are encrypted when a key is available. They are a second copy
    of money already in the transaction, so leaving them in clear would undo the
    encryption of the row they came from.
    """

    require_encryption_key(data_key, field="ledger.LedgerEntry.amount_encrypted")
    if canonical_transaction.status != CanonicalTransaction.Status.CONFIRMED:
        raise InvalidRequestError("Only confirmed transactions can be posted to the ledger.")
    if len(postings) < 2:
        raise InvalidRequestError("A ledger posting requires at least two entries.")

    transaction_currency = Currency(canonical_transaction.currency)
    debit_total = 0
    credit_total = 0
    for posting in postings:
        if posting.amount.resolved_currency != transaction_currency:
            raise InvalidRequestError("All ledger postings must use the transaction currency.")
        if posting.amount.amount_minor <= 0:
            raise InvalidRequestError("Ledger posting amounts must be positive.")
        if posting.account.user_id != canonical_transaction.user_id:
            raise InvalidRequestError("Ledger accounts must belong to the transaction owner.")
        if posting.entry_type == LedgerEntry.EntryType.DEBIT:
            debit_total += posting.amount.amount_minor
        elif posting.entry_type == LedgerEntry.EntryType.CREDIT:
            credit_total += posting.amount.amount_minor
        else:
            raise InvalidRequestError("Ledger entries must be debits or credits.")

    if debit_total != credit_total:
        raise InvalidRequestError("Ledger postings must have equal debit and credit totals.")

    with db_transaction.atomic():
        locked_transaction = CanonicalTransaction.objects.select_for_update().get(
            pk=canonical_transaction.pk
        )
        if locked_transaction.status != CanonicalTransaction.Status.CONFIRMED:
            raise InvalidRequestError("Only confirmed transactions can be posted to the ledger.")
        if LedgerEntry.objects.filter(transaction=locked_transaction).exists():
            raise ConflictError("This transaction has already been posted to the ledger.")
        pending = []
        for posting in postings:
            entry = LedgerEntry(
                transaction=locked_transaction,
                account=posting.account,
                entry_type=posting.entry_type,
                amount_encrypted=serialize_money(posting.amount),
                currency=posting.amount.resolved_currency.code,
            )
            if data_key is not None:
                # The identifier already exists — the primary key has a UUID
                # default — so the associated data can bind the ciphertext to
                # this entry before it is ever written. The entry works its own
                # owner out from the transaction it points at, which is the one
                # ``entry_amount`` will pass back when reading it.
                entry.encrypt_fields(
                    {"amount_encrypted": serialize_money(posting.amount)},
                    key=data_key,
                    key_version=key_version,
                )
            pending.append(entry)
        entries = LedgerEntry.objects.bulk_create(pending)
    return entries


def entry_amount(entry: LedgerEntry, *, data_key: bytes | None = None) -> Money:
    """The amount on one ledger entry, decrypting only if it has to.

    An entry belongs to whoever owns its transaction, so that owner supplies the
    associated data — the same one used when the value was written.
    """

    return deserialize_money(entry.read_field("amount_encrypted", key=data_key))


#: The opposite side of each entry type. Reversal is this and nothing else — a
#: debit is undone by a credit of the same amount against the same account.
OPPOSITE_ENTRY_TYPE: dict[str, str] = {
    LedgerEntry.EntryType.DEBIT: LedgerEntry.EntryType.CREDIT,
    LedgerEntry.EntryType.CREDIT: LedgerEntry.EntryType.DEBIT,
}


def transaction_net_minor(
    canonical_transaction: CanonicalTransaction, *, data_key: bytes | None = None
) -> int:
    """Debits minus credits across every entry of one transaction.

    Zero is what a fully reversed transaction looks like, and it is also what a
    correctly posted one looks like — a balanced posting has equal sides by
    construction. The number is useful for the reversal check precisely because
    it stays zero: a reversal that got an account or an amount wrong moves it.
    """

    total = 0
    for entry in LedgerEntry.objects.filter(transaction=canonical_transaction):
        amount = entry_amount(entry, data_key=data_key)
        if entry.entry_type == LedgerEntry.EntryType.DEBIT:
            total += amount.amount_minor
        else:
            total -= amount.amount_minor
    return total


def reverse_transaction_postings(
    canonical_transaction: CanonicalTransaction,
    *,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> list[LedgerEntry]:
    """Write the mirror image of every entry this transaction already has.

    The ledger is append-only. Undoing a posting by deleting its rows would
    leave a set of books that cannot explain itself: the money would be gone
    from the totals with nothing saying it had ever been there. So the original
    entries stay and an opposing entry is written for each, which is what a
    ledger has always done and what makes the reversal itself auditable.

    Reversing twice is refused rather than tolerated. A second pass would leave
    the account balanced but the entry count doubled, and every later reader
    would have to know that half the rows are noise.
    """

    require_encryption_key(data_key, field="ledger.LedgerEntry.amount_encrypted")
    with db_transaction.atomic():
        locked_transaction = CanonicalTransaction.objects.select_for_update().get(
            pk=canonical_transaction.pk
        )
        existing = list(
            LedgerEntry.objects.select_for_update().filter(transaction=locked_transaction)
        )
        if not existing:
            return []
        if transaction_net_minor(locked_transaction, data_key=data_key) != 0:
            raise ConflictError("This transaction's postings do not balance and cannot be undone.")
        if _is_already_reversed(existing, data_key=data_key):
            raise ConflictError("This transaction has already been reversed.")

        pending = []
        for entry in existing:
            amount = entry_amount(entry, data_key=data_key)
            reversal = LedgerEntry(
                transaction=locked_transaction,
                account=entry.account,
                entry_type=OPPOSITE_ENTRY_TYPE[entry.entry_type],
                amount_encrypted=serialize_money(amount),
                currency=entry.currency,
            )
            if data_key is not None:
                reversal.encrypt_fields(
                    {"amount_encrypted": serialize_money(amount)},
                    key=data_key,
                    key_version=key_version,
                )
            pending.append(reversal)
        return LedgerEntry.objects.bulk_create(pending)


def _is_already_reversed(entries: Sequence[LedgerEntry], *, data_key: bytes | None = None) -> bool:
    """Whether every posting against every account already has its mirror.

    A balanced transaction and a reversed one both net to zero overall, so the
    total cannot tell them apart. Per account it can: an unreversed posting
    leaves that account with a non-zero position, and a reversed one does not.
    """

    per_account: dict[Any, int] = {}
    for entry in entries:
        amount = entry_amount(entry, data_key=data_key).amount_minor
        signed = amount if entry.entry_type == LedgerEntry.EntryType.DEBIT else -amount
        per_account[entry.account_id] = per_account.get(entry.account_id, 0) + signed
    return all(total == 0 for total in per_account.values())
