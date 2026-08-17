"""Turning two rows of one internal move into a single transfer event.

A transfer between a user's own accounts is recorded twice — money leaving the
checking app, money arriving in the savings app — and it is one event. Accepting
the two rows separately would produce two canonical transactions, and reporting
would read them as a purchase and a payday that never happened
(specification 7.4, 17.3).

So confirmation happens here rather than through the ordinary acceptance path:
one :class:`~apps.transactions.models.CanonicalTransaction` is created, both
observations point at it, and the type is ``internal_transfer``, which
:mod:`apps.transactions.classification` excludes from income and from spending.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, ForbiddenError
from apps.ledger.rules import PostingRuleAccounts, post_transaction_by_type
from apps.observations.models import ImportedObservation
from apps.observations.review import decrypt_observation
from apps.transactions.invariants import validate_transaction_invariants
from apps.transactions.models import CanonicalTransaction

from .models import ReconciliationMatch
from .services import ReconciliationError, lock_match


def _lock_observation(observation_id: Any, user: Any) -> ImportedObservation:
    locked = (
        ImportedObservation.objects.select_for_update()
        .filter(pk=observation_id, user_id=user.pk)
        .first()
    )
    if locked is None:
        raise ForbiddenError("This observation belongs to another user.")
    return locked


def _split_sides(
    first: ImportedObservation, second: ImportedObservation
) -> tuple[ImportedObservation, ImportedObservation]:
    """Order the pair as (money out, money in).

    A match stores its two rows in a fixed order that has nothing to do with
    direction, so which side is which is read from the rows themselves.
    """

    debit = ImportedObservation.Direction.DEBIT
    credit = ImportedObservation.Direction.CREDIT
    if first.direction == debit and second.direction == credit:
        return first, second
    if second.direction == debit and first.direction == credit:
        return second, first
    raise ReconciliationError(
        "An internal transfer needs one row leaving an account and one arriving."
    )


@db_transaction.atomic
def confirm_internal_transfer(
    match_id: Any,
    *,
    user: Any,
    data_key: bytes,
    ledger_accounts: PostingRuleAccounts | None = None,
) -> CanonicalTransaction:
    """Confirm one internal-transfer candidate as a single canonical event.

    Repeating the call returns the transfer already created rather than making
    a second one, and any ledger posting happens in the same database
    transaction as the confirmation.
    """

    match = lock_match(match_id, user)
    if match.match_type != ReconciliationMatch.MatchType.INTERNAL_TRANSFER:
        raise ReconciliationError("Only internal-transfer candidates confirm a transfer event.")
    if match.status == ReconciliationMatch.Status.REJECTED:
        raise ConflictError("This candidate was already rejected.")

    left = _lock_observation(match.left_observation_id, user)
    right = _lock_observation(match.right_observation_id, user)

    if match.status == ReconciliationMatch.Status.CONFIRMED:
        # Idempotent: both sides already point at the transfer they created.
        existing = left.canonical_transaction
        if existing is None:
            raise ConflictError("This candidate was confirmed without a transfer event.")
        return existing

    outgoing, incoming = _split_sides(left, right)

    for side in (outgoing, incoming):
        if side.review_status in ImportedObservation.RESOLVED_STATUSES:
            raise ConflictError("Rejected or merged observations cannot become a transfer.")
        if side.canonical_transaction_id is not None:
            # One side already became its own transaction. Silently absorbing it
            # would leave that transaction counted as spending or income.
            raise ConflictError(
                "One side of this transfer was already accepted on its own; "
                "void that transaction before confirming the transfer."
            )

    # Both sides must sit in accounts this user owns, and in different ones.
    # This is what keeps a payment to someone else's account from being
    # recorded as an internal move.
    source = outgoing.financial_account_guess
    destination = incoming.financial_account_guess
    if source is None or destination is None:
        raise ReconciliationError(
            "Both sides of an internal transfer must be mapped to an owned account."
        )
    if source.user_id != user.pk or destination.user_id != user.pk:
        raise ForbiddenError("Both accounts must belong to the requesting user.")
    if source.pk == destination.pk:
        raise ReconciliationError("An internal transfer needs two different accounts.")

    amount = decrypt_observation(outgoing, user=user, data_key=data_key).amount
    if amount is None or amount.amount_minor <= 0:
        raise ReconciliationError("A transfer without a usable amount cannot be confirmed.")
    if outgoing.occurred_at is None:
        raise ReconciliationError("A transfer without a date cannot be confirmed.")

    # Two apps routinely disagree about the day, and either side may carry the
    # earlier one — banks show an arrival before the withdrawal clears often
    # enough. The move began when the first side recorded it and completed when
    # the last did, which is also the only ordering the model accepts.
    known_dates = sorted(
        value for value in (outgoing.occurred_at, incoming.occurred_at) if value is not None
    )
    occurred_at, posted_at = known_dates[0], known_dates[-1]

    canonical = CanonicalTransaction(
        user_id=user.pk,
        created_by=user,
        reviewed_by=user,
        occurred_at=occurred_at,
        posted_at=posted_at,
        amount_encrypted=f"{amount.amount_minor}:{amount.resolved_currency.code}",
        currency=amount.resolved_currency.code,
        transaction_type=CanonicalTransaction.TransactionType.INTERNAL_TRANSFER,
        financial_account=source,
        status=CanonicalTransaction.Status.DRAFT,
    )
    try:
        validate_transaction_invariants(canonical)
    except ValidationError as exc:
        raise ReconciliationError(f"The transfer event is invalid: {exc}") from exc
    canonical.save()

    if ledger_accounts is not None:
        # Confirm before posting: the ledger only accepts confirmed rows, and a
        # failure here rolls the whole confirmation back with it.
        canonical.status = CanonicalTransaction.Status.CONFIRMED
        canonical.save(update_fields=["status", "updated_at"])
        post_transaction_by_type(canonical, ledger_accounts)

    reviewed_at = timezone.now()
    for side in (outgoing, incoming):
        # ``transaction_type_guess`` stays as the parser left it. It is
        # provenance, and overwriting it with the confirmed answer would erase
        # the record of what the parser got wrong.
        side.canonical_transaction = canonical
        side.review_status = (
            ImportedObservation.ReviewStatus.CORRECTED
            if side.corrected_fields
            else ImportedObservation.ReviewStatus.ACCEPTED
        )
        side.reviewed_by = user
        side.reviewed_at = reviewed_at
        side.save(
            update_fields=[
                "canonical_transaction",
                "review_status",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )

    match.status = ReconciliationMatch.Status.CONFIRMED
    match.reviewed_by = user
    match.reviewed_at = reviewed_at
    match.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    record_audit_event(
        user=user,
        event_type="internal_transfer_confirmed",
        obj=match,
        metadata={
            "canonical_transaction_id": str(canonical.pk),
            "outgoing_observation_id": str(outgoing.pk),
            "incoming_observation_id": str(incoming.pk),
            "score": match.match_score,
            "posted": ledger_accounts is not None,
        },
    )
    return canonical
