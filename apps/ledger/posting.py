from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction as db_transaction

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


def post_balanced_transaction(
    canonical_transaction: CanonicalTransaction,
    postings: Sequence[Posting],
) -> list[LedgerEntry]:
    """Atomically post a confirmed transaction when debit and credit totals balance."""

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
        entries = LedgerEntry.objects.bulk_create(
            [
                LedgerEntry(
                    transaction=locked_transaction,
                    account=posting.account,
                    entry_type=posting.entry_type,
                    amount_encrypted=serialize_money(posting.amount),
                    currency=posting.amount.resolved_currency.code,
                )
                for posting in postings
            ]
        )
    return entries
