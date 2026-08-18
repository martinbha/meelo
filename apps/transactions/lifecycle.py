"""What may happen to a confirmed financial event, and what may not.

A transaction moves through three states. ``draft`` is a proposal — a reviewer
accepted an observation, or someone typed a row in, and nothing has been posted.
``confirmed`` is history: the ledger has it, reports count it, and it is the
answer to "what did I spend in March". ``voided`` is history the owner withdrew,
kept rather than deleted so the withdrawal itself is visible.

The rule the specification states in one line — "do not silently rewrite
confirmed history" (18) — is what this module enforces. Two mechanisms:

**Transitions are a table, not a comparison.** ``draft`` may become confirmed or
voided, ``confirmed`` may only be voided, and ``voided`` is terminal. Anything
else raises and changes nothing. Written as a table because the alternative is
an ``if`` in whichever service happens to be changing the status, and the
second such ``if`` is where the two disagree.

**Editing confirmed history goes through a named door.** ``update_manual_transaction``
refuses anything that is not a draft. A confirmed row can still be corrected —
people do misread receipts — but only through :func:`correct_confirmed_transaction`,
which records what changed. The point is not that corrections are rare. It is
that a correction leaves a trace and an ordinary edit does not, so the two must
not share a code path.

A correction may not touch the ledger. Amount, currency, type, account, and
instrument are what the postings were built from, and changing one under a
posted transaction leaves entries that no longer describe it. Those corrections
need a reversal, which is #153.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction as db_transaction

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, InvalidRequestError
from apps.ledger.models import LedgerEntry

from .models import CanonicalTransaction

#: Every legal move. A status missing from a value set cannot be reached from
#: that key, and ``voided`` reaches nothing at all.
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    CanonicalTransaction.Status.DRAFT: frozenset(
        {CanonicalTransaction.Status.CONFIRMED, CanonicalTransaction.Status.VOIDED}
    ),
    CanonicalTransaction.Status.CONFIRMED: frozenset({CanonicalTransaction.Status.VOIDED}),
    CanonicalTransaction.Status.VOIDED: frozenset(),
}

#: The audit event each arrival records. Voiding is not deletion: the row stays,
#: reports stop counting it, and ``transaction_deleted`` is reserved for the
#: removal path that also reverses the ledger.
STATUS_AUDIT_EVENTS: dict[str, str] = {
    CanonicalTransaction.Status.CONFIRMED: "transaction_accepted",
    CanonicalTransaction.Status.VOIDED: "transaction_voided",
}

#: The only status an ordinary edit may touch.
EDITABLE_STATUSES: frozenset[str] = frozenset({CanonicalTransaction.Status.DRAFT})

#: Fields the ledger postings were derived from. Correcting one of these on a
#: posted transaction would leave entries describing a transaction that no
#: longer exists.
LEDGER_BEARING_FIELDS: frozenset[str] = frozenset(
    {
        "amount_minor",
        "currency",
        "transaction_type",
        "financial_account",
        "payment_instrument",
    }
)

#: What a correction is allowed to change, and how to read the current value off
#: the transaction so the two can be compared. Kept as a mapping rather than
#: ``**kwargs`` so an unknown field is a refusal instead of a silent no-op.
CORRECTABLE_FIELDS: dict[str, str] = {
    "occurred_at": "occurred_at",
    "posted_at": "posted_at",
    "transaction_type": "transaction_type",
    "category": "category_id",
    "financial_account": "financial_account_id",
    "payment_instrument": "payment_instrument_id",
}


class TransitionError(ConflictError):
    """The requested status change is not one the lifecycle allows."""


class ImmutableTransactionError(ConflictError):
    """This transaction is no longer editable in place."""


def is_posted(transaction: CanonicalTransaction) -> bool:
    return LedgerEntry.objects.filter(transaction=transaction).exists()


def assert_editable_in_place(transaction: CanonicalTransaction) -> None:
    """Refuse an ordinary edit of anything that is no longer a proposal.

    The old rule was "confirmed *and* posted", which left a window: a confirmed
    transaction that had not reached the ledger yet could be rewritten with no
    record that it had ever said something else. Reports already counted it by
    then. Confirmation is the line, not posting.
    """

    if transaction.status in EDITABLE_STATUSES:
        return
    if transaction.status == CanonicalTransaction.Status.VOIDED:
        raise ImmutableTransactionError("A voided transaction cannot be edited.")
    raise ImmutableTransactionError(
        "A confirmed transaction cannot be edited in place. Record a correction instead."
    )


@db_transaction.atomic
def transition_transaction_status(
    transaction_id: Any,
    *,
    user: Any,
    status: str,
    data_key: bytes | None = None,
) -> CanonicalTransaction:
    """Move one transaction to a new status, or refuse and change nothing."""

    # Imported here rather than at module scope: invariants validates a
    # transaction's contents, this module governs its lifecycle, and the
    # contents check needs to run inside the lock the lifecycle takes.
    from .invariants import validate_transaction_invariants

    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(pk=transaction_id, user=user)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    if status not in ALLOWED_STATUS_TRANSITIONS:
        raise TransitionError(f"'{status}' is not a transaction status.")
    previous_status = transaction.status
    if status not in ALLOWED_STATUS_TRANSITIONS[previous_status]:
        raise TransitionError(
            f"Cannot change transaction status from {previous_status} to {status}."
        )
    transaction.status = status
    transaction.reviewed_by = user
    validate_transaction_invariants(transaction, data_key=data_key)
    transaction.save(update_fields=["status", "reviewed_by", "updated_at"])
    record_audit_event(
        user=user,
        event_type=STATUS_AUDIT_EVENTS[status],
        obj=transaction,
        metadata={"status": status, "previous_status": previous_status},
    )
    return transaction


@db_transaction.atomic
def correct_confirmed_transaction(
    transaction_id: Any,
    *,
    user: Any,
    reason: str,
    **changes: Any,
) -> tuple[CanonicalTransaction, tuple[str, ...]]:
    """The named door: change a confirmed transaction and say so.

    Returns the transaction and the names of the fields that actually changed.
    A reason is required — not for the machine, which does not read it, but
    because "why is this number different from the receipt" is the question the
    audit log exists to answer, and only the person making the change knows.

    Values never reach the audit record. Field *names* do. Specification 23 is
    explicit that audit logs must not contain financial plaintext, and "the
    amount was changed" is exactly as useful for reconstructing what happened
    without also being a readable copy of the amount.
    """

    if not reason.strip():
        raise InvalidRequestError("A correction requires a reason.")
    unknown = set(changes) - set(CORRECTABLE_FIELDS)
    if unknown:
        raise InvalidRequestError(
            f"These fields cannot be corrected: {', '.join(sorted(unknown))}."
        )

    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(pk=transaction_id, user=user)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    if transaction.status != CanonicalTransaction.Status.CONFIRMED:
        raise ImmutableTransactionError(
            "Only a confirmed transaction is corrected; a draft is simply edited."
        )

    changed: list[str] = []
    for field, value in changes.items():
        current = getattr(transaction, CORRECTABLE_FIELDS[field])
        replacement = getattr(value, "pk", value)
        if current != replacement:
            changed.append(field)
    if not changed:
        return transaction, ()

    ledger_bearing = sorted(set(changed) & LEDGER_BEARING_FIELDS)
    if ledger_bearing and is_posted(transaction):
        raise ConflictError(
            "This transaction is posted to the ledger, so "
            f"{', '.join(ledger_bearing)} cannot be corrected in place. "
            "Reverse it and enter the corrected transaction instead."
        )

    for field in changed:
        setattr(transaction, field, changes[field])
    transaction.reviewed_by = user
    transaction.full_clean(validate_constraints=False)
    transaction.save(update_fields=[*changed, "reviewed_by", "updated_at"])

    record_audit_event(
        user=user,
        event_type="transaction_corrected",
        obj=transaction,
        # Names, never values. See the docstring.
        metadata={
            "changed_fields": sorted(changed),
            "reason": reason.strip()[:200],
            "status": transaction.status,
        },
    )
    return transaction, tuple(sorted(changed))
