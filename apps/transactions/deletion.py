"""Removing a transaction that should never have existed.

"Delete" is the word a person uses; it is not what happens. The row stays, its
ledger entries stay, and an opposing entry is written for each of them. What
changes is that the transaction becomes ``voided``, so reports stop counting it,
and every observation that fed it goes back into the review queue.

Deleting the rows instead would be the obvious implementation and the wrong one.
A set of books that can be edited backwards cannot explain itself: the money
would be missing from the totals with nothing recording that it had ever been
there, and the one question worth asking afterwards — "what did I remove, and
when" — would have no answer.

Three things happen together or not at all: the reversal, the void, and the
release of the observations. A reversal without the void leaves a transaction
that reports still count against a ledger that says it never happened. A void
without the release strands the observations forever — accepted, pointing at a
transaction nobody can see, and out of the queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, InvalidRequestError
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import reverse_transaction_postings, transaction_net_minor

from .lifecycle import transition_transaction_status
from .models import CanonicalTransaction


class DeletionNotConfirmedError(ConflictError):
    """Deletion was requested without the explicit confirmation it requires."""


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """What the deletion did, for the caller's message and for the tests."""

    transaction: CanonicalTransaction
    reversal_entry_count: int
    released_observation_count: int


def _release_observations(transaction: CanonicalTransaction, *, user: Any) -> int:
    """Put every observation that fed this transaction back in the queue.

    Imported here rather than at module scope: observations depend on
    transactions, so the import has to run after both apps are loaded or the
    two modules import each other.
    """

    from apps.observations.models import ImportedObservation

    linked = ImportedObservation.objects.select_for_update().filter(
        canonical_transaction=transaction, user_id=user.pk
    )
    return linked.update(
        canonical_transaction=None,
        review_status=ImportedObservation.ReviewStatus.UNREVIEWED,
        reviewed_by=None,
        reviewed_at=None,
        updated_at=timezone.now(),
    )


@db_transaction.atomic
def delete_transaction(
    transaction_id: Any,
    *,
    user: Any,
    reason: str = "",
    confirmed: bool = False,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> DeletionResult:
    """Void a transaction, reverse its postings, and free its observations.

    ``confirmed`` is not ceremony. This is the one operation in the application
    that removes a confirmed financial event from every report at once, and it
    cannot be undone by repeating it — the reversal is already written. A caller
    that has not said so explicitly is refused.
    """

    if not confirmed:
        raise DeletionNotConfirmedError("Deleting a transaction requires explicit confirmation.")

    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(pk=transaction_id, user_id=user.pk)
        .first()
    )
    if transaction is None:
        # Deliberately the same answer as a transaction that does not exist.
        # Someone probing identifiers has no business learning which of the two
        # they found.
        raise InvalidRequestError("Transaction not found.")
    if transaction.status == CanonicalTransaction.Status.VOIDED:
        raise ConflictError("This transaction has already been deleted.")

    reversals = reverse_transaction_postings(
        transaction, data_key=data_key, key_version=key_version
    )
    released = _release_observations(transaction, user=user)
    transaction = transition_transaction_status(
        transaction.pk,
        user=user,
        status=CanonicalTransaction.Status.VOIDED,
        data_key=data_key,
    )

    record_audit_event(
        user=user,
        event_type="transaction_deleted",
        obj=transaction,
        # Counts and a reason. No amounts, no merchant, no account name —
        # specification 23 keeps financial plaintext out of the audit log, and a
        # deletion record is not an exception to that.
        metadata={
            "reversal_entry_count": len(reversals),
            "released_observation_count": released,
            "reason": reason.strip()[:200],
        },
    )
    return DeletionResult(
        transaction=transaction,
        reversal_entry_count=len(reversals),
        released_observation_count=released,
    )


def is_ledger_balanced_after_deletion(
    transaction: CanonicalTransaction, *, data_key: bytes | None = None
) -> bool:
    """Whether this transaction's entries now cancel out completely."""

    if not LedgerEntry.objects.filter(transaction=transaction).exists():
        return True
    return transaction_net_minor(transaction, data_key=data_key) == 0
