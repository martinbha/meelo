from __future__ import annotations

from django.core.exceptions import ValidationError

from apps.core.crypto import EncryptionError
from apps.core.errors import InvalidRequestError
from apps.core.value_objects import Currency
from apps.ledger.models import LedgerEntry

from .models import CanonicalTransaction
from .money import read_money


def validate_transaction_invariants(
    transaction: CanonicalTransaction, *, data_key: bytes | None = None
) -> None:
    """Run all model-level checks plus amount and posting safety checks.

    Database constraints are deliberately not pre-checked here. The idempotency
    key is designed to collide when an attempt is repeated, and
    :func:`apps.transactions.idempotency.save_once` resolves that collision by
    returning the transaction the winner made. Raising on it first would turn a
    converged retry into an error nobody can act on.
    """

    transaction.full_clean(validate_constraints=False)
    try:
        amount = read_money(transaction, "amount_encrypted", data_key=data_key)
    except (InvalidRequestError, EncryptionError) as exc:
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
