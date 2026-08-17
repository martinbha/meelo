from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction as db_transaction

from apps.core.crypto import encrypt_model_fields, read_model_field
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
                # this entry before it is ever written. An entry holds no owner
                # of its own, so the transaction's is supplied; ``entry_amount``
                # passes the same one back.
                encrypt_model_fields(
                    entry,
                    {"amount_encrypted": serialize_money(posting.amount)},
                    key=data_key,
                    key_version=key_version,
                    user_id=locked_transaction.user_id,
                )
            pending.append(entry)
        entries = LedgerEntry.objects.bulk_create(pending)
    return entries


def entry_amount(entry: LedgerEntry, *, data_key: bytes | None = None) -> Money:
    """The amount on one ledger entry, decrypting only if it has to.

    An entry belongs to whoever owns its transaction, so that owner supplies the
    associated data — the same one used when the value was written.
    """

    return deserialize_money(
        read_model_field(entry, "amount_encrypted", key=data_key, user_id=entry.transaction.user_id)
    )
