from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.value_objects import Currency
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import deserialize_money

from .models import CanonicalTransaction

ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    CanonicalTransaction.Status.DRAFT: frozenset(
        {CanonicalTransaction.Status.CONFIRMED, CanonicalTransaction.Status.VOIDED}
    ),
    CanonicalTransaction.Status.CONFIRMED: frozenset({CanonicalTransaction.Status.VOIDED}),
    CanonicalTransaction.Status.VOIDED: frozenset(),
}


def validate_transaction_invariants(transaction: CanonicalTransaction) -> None:
    """Run all model-level checks plus amount and posting safety checks."""

    transaction.full_clean()
    try:
        amount = deserialize_money(transaction.amount_encrypted)
    except InvalidRequestError as exc:
        raise ValidationError({"amount_encrypted": str(exc)}) from exc
    if amount.resolved_currency != Currency(transaction.currency):
        raise ValidationError(
            {"amount_encrypted": "Amount currency must match transaction currency."}
        )
    if amount.amount_minor <= 0:
        raise ValidationError({"amount_encrypted": "Amount must be positive."})
    if (
        transaction.status != CanonicalTransaction.Status.CONFIRMED
        and LedgerEntry.objects.filter(transaction=transaction).exists()
    ):
        raise ValidationError({"status": "Only confirmed transactions may have ledger entries."})


@db_transaction.atomic
def transition_transaction_status(
    transaction_id: Any,
    *,
    user: Any,
    status: str,
) -> CanonicalTransaction:
    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(pk=transaction_id, user=user)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    allowed = ALLOWED_STATUS_TRANSITIONS[transaction.status]
    if status not in allowed:
        raise ConflictError(
            f"Cannot change transaction status from {transaction.status} to {status}."
        )
    transaction.status = status
    transaction.reviewed_by = user
    validate_transaction_invariants(transaction)
    transaction.save(update_fields=["status", "reviewed_by", "updated_at"])
    record_audit_event(
        user=user,
        event_type=(
            "transaction_accepted"
            if status == CanonicalTransaction.Status.CONFIRMED
            else "transaction_deleted"
        ),
        obj=transaction,
        metadata={"status": status},
    )
    return transaction
