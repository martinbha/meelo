"""Connecting a refund to the purchase it reverses.

A refund is not income. The user who bought a coat for 200,000 and returned it
is not 200,000 better off than they started — they are back where they began,
and the coat's category should show nothing. Counting the credit as income
would inflate income and spending at once while leaving the category wrong
(specification 7.5, 17.4).

So a confirmed refund becomes a ``refund`` transaction carrying *the purchase's
category*, which :mod:`apps.transactions.classification` subtracts from that
category rather than adding to income.

Refunds that no purchase claims are not hidden. They stay in the review queue
for the user to classify, because an unmatched credit really might be income.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, ForbiddenError
from apps.ledger.rules import PostingRuleAccounts, post_transaction_by_type
from apps.observations.models import ImportedObservation
from apps.observations.review import decrypt_observation
from apps.transactions.idempotency import REFUND_SOURCE, save_once, source_key
from apps.transactions.invariants import validate_transaction_invariants
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money

from .matching import MatchProposal, match_refund_to_purchase
from .models import ReconciliationMatch
from .services import ReconciliationError, facts_from, lock_match, record_proposals

#: Statuses a match holds while it still speaks for its two rows.
_LIVE_STATUSES = (ReconciliationMatch.Status.PROPOSED, ReconciliationMatch.Status.CONFIRMED)


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
    """Order the pair as (money back, money originally spent)."""

    debit = ImportedObservation.Direction.DEBIT
    credit = ImportedObservation.Direction.CREDIT
    if first.direction == credit and second.direction == debit:
        return first, second
    if second.direction == credit and first.direction == debit:
        return second, first
    raise ReconciliationError("A refund match needs one credit row and one debit row.")


def propose_refund_matches(
    *,
    user: Any,
    data_key: bytes,
    key_version: int = 1,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[ReconciliationMatch, ...]:
    """Pair credit rows still awaiting review against the purchases they reverse.

    The purchase side deliberately includes rows already accepted: a refund
    usually arrives weeks after its purchase was reviewed, and requiring both
    sides to be open would miss almost every real case.
    """

    rows = ImportedObservation.objects.filter(user_id=user.pk, occurred_at__isnull=False).exclude(
        review_status__in=ImportedObservation.RESOLVED_STATUSES
    )
    if start_date is not None:
        rows = rows.filter(occurred_at__gte=start_date)
    if end_date is not None:
        rows = rows.filter(occurred_at__lte=end_date)
    candidates = list(rows)
    facts = {}
    for row in candidates:
        values = decrypt_observation(row, user=user, data_key=data_key)
        facts[row.pk] = facts_from(row, merchant=values.merchant, amount_minor=values.amount_minor)
    refunds = [
        row
        for row in candidates
        if row.direction == ImportedObservation.Direction.CREDIT
        and row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED
        and row.canonical_transaction_id is None
    ]
    purchases = [row for row in candidates if row.direction == ImportedObservation.Direction.DEBIT]

    proposals: list[MatchProposal] = []
    for refund in refunds:
        for purchase in purchases:
            proposal = match_refund_to_purchase(facts[refund.pk], facts[purchase.pk])
            if proposal is not None:
                proposals.append(proposal)
    return record_proposals(
        user=user, proposals=proposals, data_key=data_key, key_version=key_version
    )


def unmatched_refunds(user: Any) -> QuerySet[ImportedObservation]:
    """Credit rows waiting for review that no refund candidate claims.

    An unmatched credit is still a real event and must stay visible: it might
    be a refund whose purchase was never screenshotted, and it might be income.
    Only the user can say which, so nothing here decides for them.
    """

    claimed = ReconciliationMatch.objects.filter(
        user_id=user.pk,
        match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
        status__in=_LIVE_STATUSES,
    )
    claimed_ids = {
        observation_id
        for pair in claimed.values_list("left_observation_id", "right_observation_id")
        for observation_id in pair
    }
    return (
        ImportedObservation.objects.filter(
            user_id=user.pk,
            direction=ImportedObservation.Direction.CREDIT,
            review_status=ImportedObservation.ReviewStatus.UNREVIEWED,
            canonical_transaction__isnull=True,
        )
        .exclude(pk__in=claimed_ids)
        .order_by("-occurred_at", "row_index", "pk")
    )


def _dismiss_competing_candidates(
    confirmed: ReconciliationMatch, *, refund: ImportedObservation, user: Any
) -> list[ReconciliationMatch]:
    """Close the other purchases this refund was also paired with.

    One refund can resemble several purchases at once — the same shop, the same
    amount, a fortnight apart. Once the user has said which one it reverses the
    rest are answered, and leaving them open would ask the same question again
    every time the queue is opened.
    """

    competing = list(
        ReconciliationMatch.objects.select_for_update()
        .filter(
            user_id=user.pk,
            match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
            status=ReconciliationMatch.Status.PROPOSED,
        )
        .filter(Q(left_observation_id=refund.pk) | Q(right_observation_id=refund.pk))
        .exclude(pk=confirmed.pk)
    )
    for candidate in competing:
        candidate.status = ReconciliationMatch.Status.REJECTED
        candidate.reviewed_by = user
        candidate.reviewed_at = confirmed.reviewed_at
        candidate.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        record_audit_event(
            user=user,
            event_type="reconciliation_match_rejected",
            obj=candidate,
            metadata={
                "match_type": candidate.match_type,
                "score": candidate.match_score,
                "superseded_by": str(confirmed.pk),
            },
        )
    return competing


def _purchase_category(purchase: ImportedObservation) -> Any:
    """The category the refund has to give back to.

    A purchase already accepted carries the category the user confirmed, which
    outranks whatever the parser guessed on the row.
    """

    if purchase.canonical_transaction is not None:
        return purchase.canonical_transaction.category
    return purchase.category_guess


@db_transaction.atomic
def confirm_refund_match(
    match_id: Any,
    *,
    user: Any,
    data_key: bytes,
    ledger_accounts: PostingRuleAccounts | None = None,
    key_version: int = 1,
) -> CanonicalTransaction:
    """Confirm one refund candidate as a refund against its purchase's category."""

    match = lock_match(match_id, user)
    if match.match_type != ReconciliationMatch.MatchType.REFUND_MATCH:
        raise ReconciliationError("Only refund candidates confirm a refund event.")
    if match.status == ReconciliationMatch.Status.REJECTED:
        raise ConflictError("This candidate was already rejected.")

    left = _lock_observation(match.left_observation_id, user)
    right = _lock_observation(match.right_observation_id, user)
    refund, purchase = _split_sides(left, right)

    if match.status == ReconciliationMatch.Status.CONFIRMED:
        existing = refund.canonical_transaction
        if existing is None:
            raise ConflictError("This candidate was confirmed without a refund event.")
        return existing  # Idempotent.

    if refund.review_status in ImportedObservation.RESOLVED_STATUSES:
        raise ConflictError("A rejected or merged row cannot become a refund.")
    if refund.canonical_transaction_id is not None:
        raise ConflictError(
            "This row was already accepted on its own; void that transaction "
            "before confirming the refund."
        )

    account = refund.financial_account_guess or purchase.financial_account_guess
    if account is None:
        raise ReconciliationError("A refund needs a financial account to post against.")
    if account.user_id != user.pk:
        raise ForbiddenError("The financial account belongs to another user.")

    instrument = refund.payment_instrument_guess
    if instrument is not None and instrument.financial_account_id != account.pk:
        raise ReconciliationError(
            "The payment instrument is not compatible with the selected account."
        )

    values = decrypt_observation(refund, user=user, data_key=data_key)
    amount = values.amount
    if amount is None or amount.amount_minor <= 0:
        raise ReconciliationError("A refund without a usable amount cannot be confirmed.")
    if refund.occurred_at is None:
        raise ReconciliationError("A refund without a date cannot be confirmed.")

    # A posted date earlier than the occurred date is not a valid row, and a
    # parser that read the two the wrong way round should not block the refund.
    posted_at = refund.posted_at
    if posted_at is not None and posted_at < refund.occurred_at:
        posted_at = None

    canonical = CanonicalTransaction(
        user_id=user.pk,
        created_by=user,
        reviewed_by=user,
        occurred_at=refund.occurred_at,
        posted_at=posted_at,
        currency=amount.resolved_currency.code,
        # Always a refund, never income. The credit direction alone cannot tell
        # the two apart, and guessing wrong inflates income permanently.
        transaction_type=CanonicalTransaction.TransactionType.REFUND,
        financial_account=account,
        payment_instrument=instrument,
        category=_purchase_category(purchase),
        status=CanonicalTransaction.Status.DRAFT,
        source_idempotency_key=source_key(REFUND_SOURCE, match.pk),
    )
    # Encrypted under this row's identity before anything is written: the
    # observations these came from held their values encrypted, and copying them
    # out in clear would undo that at the moment they became history.
    store_money(canonical, "amount_encrypted", amount, data_key=data_key, key_version=key_version)
    canonical.encrypt_fields(
        {"merchant_encrypted": values.merchant}, key=data_key, key_version=key_version
    )
    try:
        validate_transaction_invariants(canonical, data_key=data_key)
    except ValidationError as exc:
        raise ReconciliationError(f"The refund event is invalid: {exc}") from exc
    canonical, created = save_once(canonical)
    if not created:
        # An earlier attempt already recorded this refund; posting again would
        # double its ledger entries.
        ledger_accounts = None

    if ledger_accounts is not None:
        canonical.status = CanonicalTransaction.Status.CONFIRMED
        canonical.save(update_fields=["status", "updated_at"])
        post_transaction_by_type(
            canonical, ledger_accounts, data_key=data_key, key_version=key_version
        )

    refund.canonical_transaction = canonical
    refund.review_status = (
        ImportedObservation.ReviewStatus.CORRECTED
        if refund.corrected_fields
        else ImportedObservation.ReviewStatus.ACCEPTED
    )
    refund.reviewed_by = user
    refund.reviewed_at = timezone.now()
    refund.save(
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
    match.reviewed_at = refund.reviewed_at
    match.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    superseded = _dismiss_competing_candidates(match, refund=refund, user=user)

    record_audit_event(
        user=user,
        event_type="refund_matched",
        obj=match,
        metadata={
            "canonical_transaction_id": str(canonical.pk),
            "refund_observation_id": str(refund.pk),
            "purchase_observation_id": str(purchase.pk),
            "category_inherited": canonical.category_id is not None,
            "superseded_candidates": len(superseded),
            "score": match.match_score,
            "posted": ledger_accounts is not None,
        },
    )
    return canonical
