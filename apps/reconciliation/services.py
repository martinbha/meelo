"""Storing, confirming, and rejecting reconciliation candidates.

Detection produces proposals; this module is where they become rows a reviewer
can act on. Confirming a duplicate is the only path that changes an
observation, and it goes through the observations app's merge service so the
same safety rules apply — a confirmed transaction can never be merged away.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.crypto import decrypt_model_field, encrypt_model_field
from apps.core.errors import ConflictError, ForbiddenError, InvalidRequestError
from apps.observations.models import ImportedObservation
from apps.observations.review import merge_observations
from apps.processing.models import SourceDocument

from .duplicates import (
    AUTOMATIC_MERGE_ENABLED,
    DuplicateCandidate,
    ObservationFacts,
)
from .images import SimilarPair, near_duplicate_detection_enabled, sorted_pair
from .matching import MatchProposal
from .models import NearDuplicateDocument, ReconciliationMatch

#: Queue filter names the observations app understands, keyed by match type.
QUEUE_FILTER_BY_MATCH_TYPE: Mapping[str, str] = {
    ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION: "duplicate",
    ReconciliationMatch.MatchType.INTERNAL_TRANSFER: "transfer",
    ReconciliationMatch.MatchType.REFUND_MATCH: "refund",
    ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT: "settlement",
    ReconciliationMatch.MatchType.STATEMENT_MEMBERSHIP: "settlement",
}


#: A link the user made themselves carries no doubt about whether the two rows
#: belong together, so it is scored at the top of the range.
MANUAL_LINK_SCORE = 100


class ReconciliationError(InvalidRequestError):
    """A reconciliation candidate cannot be created or acted on."""


def _ordered(left: Any, right: Any) -> tuple[Any, Any]:
    """Order a pair so the same relationship is always stored the same way."""

    return (left, right) if str(left) <= str(right) else (right, left)


@db_transaction.atomic
def record_match(
    *,
    user: Any,
    left_observation_id: Any,
    right_observation_id: Any,
    match_type: str,
    score: int,
    features: Sequence[str] = (),
    data_key: bytes | None = None,
    key_version: int = 1,
) -> ReconciliationMatch:
    """Store one candidate, or update the score of one already proposed.

    Re-running detection must not pile up duplicates of duplicates, so a pair
    that already has a candidate of this type is updated in place. A candidate
    a reviewer has already decided is left alone.
    """

    if left_observation_id == right_observation_id:
        raise ReconciliationError("A match must join two different observations.")
    owned = set(
        ImportedObservation.objects.filter(
            pk__in=[left_observation_id, right_observation_id], user_id=user.pk
        ).values_list("pk", flat=True)
    )
    if owned != {left_observation_id, right_observation_id}:
        raise ForbiddenError("Both observations must belong to the requesting user.")

    left_id, right_id = _ordered(left_observation_id, right_observation_id)
    existing = (
        ReconciliationMatch.objects.select_for_update()
        .filter(left_observation_id=left_id, right_observation_id=right_id, match_type=match_type)
        .first()
    )
    if existing is not None:
        if existing.status != ReconciliationMatch.Status.PROPOSED:
            # A decision already made is not revisited by a later detection run.
            return existing
        existing.match_score = min(100, int(score))
        existing.match_features_json_encrypted = _encrypted_features(
            existing, features, data_key=data_key, key_version=key_version
        )
        existing.save(update_fields=["match_score", "match_features_json_encrypted", "updated_at"])
        return existing

    match = ReconciliationMatch(
        user_id=user.pk,
        left_observation_id=left_id,
        right_observation_id=right_id,
        match_type=match_type,
        match_score=min(100, int(score)),
    )
    match.full_clean()
    match.match_features_json_encrypted = _encrypted_features(
        match, features, data_key=data_key, key_version=key_version
    )
    match.save()
    record_audit_event(
        user=user,
        event_type="reconciliation_match_created",
        obj=match,
        metadata={
            "match_type": match_type,
            "score": match.match_score,
            # Feature names explain the pairing without repeating any value.
            "features": sorted(features),
        },
    )
    return match


def _encrypted_features(
    match: ReconciliationMatch,
    features: Sequence[str],
    *,
    data_key: bytes | None,
    key_version: int,
) -> str:
    if not features or data_key is None:
        return ""
    payload = json.dumps(sorted(features), separators=(",", ":"))
    return encrypt_model_field(
        match, "match_features_json_encrypted", payload, key=data_key, key_version=key_version
    )


def decrypt_match_features(match: ReconciliationMatch, *, data_key: bytes) -> tuple[str, ...]:
    """Read back the feature names that produced this candidate's score.

    Returns nothing rather than raising when a candidate was stored without a
    data key: a proposal with no recorded evidence should show no reasons, not
    break the queue it sits in.
    """

    if not match.match_features_json_encrypted:
        return ()
    payload = decrypt_model_field(match, "match_features_json_encrypted", key=data_key)
    try:
        values = json.loads(payload)
    except json.JSONDecodeError:
        return ()
    return tuple(str(value) for value in values)


@db_transaction.atomic
def link_observations(
    *,
    user: Any,
    left_observation_id: Any,
    right_observation_id: Any,
    match_type: str,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> ReconciliationMatch:
    """Record a relationship the user asserts, rather than one detection found.

    Scored :data:`MANUAL_LINK_SCORE` with a single ``manual_link`` reason, so
    the queue says the evidence is the user's own judgement instead of implying
    the matcher noticed something.

    A pair the user previously dismissed is reopened here. Detection must never
    resurrect a rejected candidate; the person who rejected it may.
    """

    if match_type not in ReconciliationMatch.MatchType.values:
        raise ReconciliationError("Unknown match type.")
    features = ("manual_link",)
    match = record_match(
        user=user,
        left_observation_id=left_observation_id,
        right_observation_id=right_observation_id,
        match_type=match_type,
        score=MANUAL_LINK_SCORE,
        features=features,
        data_key=data_key,
        key_version=key_version,
    )
    if match.status == ReconciliationMatch.Status.REJECTED:
        match.status = ReconciliationMatch.Status.PROPOSED
        match.match_score = MANUAL_LINK_SCORE
        match.reviewed_by = None
        match.reviewed_at = None
        match.match_features_json_encrypted = _encrypted_features(
            match, features, data_key=data_key, key_version=key_version
        )
        match.save(
            update_fields=[
                "status",
                "match_score",
                "match_features_json_encrypted",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        record_audit_event(
            user=user,
            event_type="reconciliation_match_created",
            obj=match,
            metadata={
                "match_type": match_type,
                "score": MANUAL_LINK_SCORE,
                "features": list(features),
                "reopened": True,
            },
        )
    return match


def record_duplicate_candidates(
    *,
    user: Any,
    candidates: Iterable[DuplicateCandidate],
    data_key: bytes | None = None,
    key_version: int = 1,
) -> tuple[ReconciliationMatch, ...]:
    """Persist scored duplicate pairs as candidates for review.

    Nothing is merged here even at a perfect score: automatic merging stays
    disabled for the initial release.
    """

    stored: list[ReconciliationMatch] = []
    for candidate in candidates:
        stored.append(
            record_match(
                user=user,
                left_observation_id=candidate.left.observation_id,
                right_observation_id=candidate.right.observation_id,
                match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
                score=candidate.score.score,
                features=candidate.features,
                data_key=data_key,
                key_version=key_version,
            )
        )
    return tuple(stored)


def record_proposals(
    *,
    user: Any,
    proposals: Iterable[MatchProposal],
    data_key: bytes | None = None,
    key_version: int = 1,
) -> tuple[ReconciliationMatch, ...]:
    """Persist reconciliation proposals of any type."""

    return tuple(
        record_match(
            user=user,
            left_observation_id=proposal.left_observation_id,
            right_observation_id=proposal.right_observation_id,
            match_type=proposal.match_type,
            score=proposal.score,
            features=proposal.features,
            data_key=data_key,
            key_version=key_version,
        )
        for proposal in proposals
    )


@db_transaction.atomic
def record_near_duplicates(
    *, user: Any, pairs: Iterable[SimilarPair]
) -> tuple[NearDuplicateDocument, ...]:
    """Store near-identical screenshot links, if the feature is enabled."""

    if not near_duplicate_detection_enabled():
        return ()
    stored: list[NearDuplicateDocument] = []
    for pair in pairs:
        first, second = sorted_pair(pair.document_id, pair.similar_document_id)
        owned = set(
            SourceDocument.objects.filter(pk__in=[first, second], user_id=user.pk).values_list(
                "pk", flat=True
            )
        )
        if owned != {first, second}:
            raise ForbiddenError("Both documents must belong to the requesting user.")
        link, created = NearDuplicateDocument.objects.get_or_create(
            document_id=first,
            similar_document_id=second,
            defaults={
                "user_id": user.pk,
                "distance": pair.distance,
                "algorithm": pair.algorithm,
            },
        )
        if not created and link.distance != pair.distance:
            link.distance = pair.distance
            link.save(update_fields=["distance"])
        stored.append(link)
    return tuple(stored)


def open_matches(user: Any, *, match_type: str | None = None) -> Any:
    """Candidates still awaiting a decision, scoped to their owner."""

    queryset = ReconciliationMatch.objects.filter(
        user_id=user.pk, status=ReconciliationMatch.Status.PROPOSED
    )
    if match_type is not None:
        queryset = queryset.filter(match_type=match_type)
    return queryset.order_by("-match_score", "-created_at")


def queue_match_ids(user: Any) -> dict[str, list[Any]]:
    """Observation identifiers per review-queue filter.

    The review queue takes these rather than importing this app, which keeps
    the dependency pointing one way: reconciliation knows about observations,
    not the reverse.
    """

    result: dict[str, list[Any]] = {}
    for match in open_matches(user):
        name = QUEUE_FILTER_BY_MATCH_TYPE.get(match.match_type)
        if name is None:
            continue
        bucket = result.setdefault(name, [])
        bucket.append(match.left_observation_id)
        bucket.append(match.right_observation_id)
    return {name: sorted(set(ids), key=str) for name, ids in result.items()}


@db_transaction.atomic
def confirm_duplicate_match(match_id: Any, *, user: Any, winner_id: Any) -> ReconciliationMatch:
    """Merge a confirmed duplicate pair, keeping both sources traceable.

    The merge itself is delegated to the observations app so a row backed by a
    confirmed transaction still cannot be merged away, and so a repeated
    confirmation is a no-op rather than a second merge.
    """

    match = lock_match(match_id, user)
    if match.match_type != ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION:
        raise ReconciliationError("Only duplicate candidates can be merged.")

    # The winner usually arrives from a form as a string, so the pair is
    # resolved by textual identity and the stored identifier is used from here.
    by_text = {
        str(match.left_observation_id): match.left_observation_id,
        str(match.right_observation_id): match.right_observation_id,
    }
    resolved_winner = by_text.get(str(winner_id))
    if resolved_winner is None:
        raise ReconciliationError("The winning observation must be part of this match.")
    loser_id = next(value for key, value in by_text.items() if key != str(winner_id))

    if match.status == ReconciliationMatch.Status.CONFIRMED:
        return match  # Idempotent.
    if match.status == ReconciliationMatch.Status.REJECTED:
        raise ConflictError("This candidate was already rejected.")

    merge_observations(user=user, winner_id=resolved_winner, duplicate_ids=[loser_id])

    match.status = ReconciliationMatch.Status.CONFIRMED
    match.reviewed_by = user
    match.reviewed_at = timezone.now()
    match.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    record_audit_event(
        user=user,
        event_type="duplicate_merged",
        obj=match,
        metadata={
            "winner_observation_id": str(resolved_winner),
            "merged_observation_id": str(loser_id),
            "score": match.match_score,
        },
    )
    return match


@db_transaction.atomic
def confirm_match(match_id: Any, *, user: Any) -> ReconciliationMatch:
    """Confirm a non-duplicate relationship, such as a settlement or transfer."""

    match = lock_match(match_id, user)
    if match.match_type == ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION:
        raise ReconciliationError("Duplicate candidates are confirmed through the merge workflow.")
    if match.match_type == ReconciliationMatch.MatchType.INTERNAL_TRANSFER:
        # A transfer confirmed here would leave both sides free to be accepted
        # separately, which is exactly the double count the match exists to
        # prevent. It goes through apps.reconciliation.transfers instead.
        raise ReconciliationError("Internal transfers are confirmed through the transfer workflow.")
    if match.match_type == ReconciliationMatch.MatchType.REFUND_MATCH:
        # Confirming here would leave the credit row free to be accepted as
        # income, which is the one outcome a refund must never have.
        raise ReconciliationError("Refunds are confirmed through the refund workflow.")
    if match.status == ReconciliationMatch.Status.CONFIRMED:
        return match
    if match.status == ReconciliationMatch.Status.REJECTED:
        raise ConflictError("This candidate was already rejected.")

    match.status = ReconciliationMatch.Status.CONFIRMED
    match.reviewed_by = user
    match.reviewed_at = timezone.now()
    match.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    record_audit_event(
        user=user,
        event_type="reconciliation_match_confirmed",
        obj=match,
        metadata={"match_type": match.match_type, "score": match.match_score},
    )
    return match


@db_transaction.atomic
def reject_match(match_id: Any, *, user: Any) -> ReconciliationMatch:
    """Dismiss a candidate. Both observations stay exactly as they were."""

    match = lock_match(match_id, user)
    if match.status == ReconciliationMatch.Status.REJECTED:
        return match
    if match.status == ReconciliationMatch.Status.CONFIRMED:
        raise ConflictError("A confirmed match cannot be rejected.")

    match.status = ReconciliationMatch.Status.REJECTED
    match.reviewed_by = user
    match.reviewed_at = timezone.now()
    match.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    record_audit_event(
        user=user,
        event_type="reconciliation_match_rejected",
        obj=match,
        metadata={"match_type": match.match_type, "score": match.match_score},
    )
    return match


def lock_match(match_id: Any, user: Any) -> ReconciliationMatch:
    """Take a row lock on one candidate, refusing another user's."""

    match = (
        ReconciliationMatch.objects.select_for_update().filter(pk=match_id, user_id=user.pk).first()
    )
    if match is None:
        raise ForbiddenError("This match belongs to another user.")
    return match


def automatic_merge_enabled() -> bool:
    """Whether a perfect score may merge without a person. It may not."""

    return AUTOMATIC_MERGE_ENABLED


def facts_from(
    observation: ImportedObservation,
    *,
    merchant: str = "",
    amount_minor: int | None = None,
    approval_code: str = "",
    balance_after_minor: int | None = None,
    source_type: str = "",
) -> ObservationFacts:
    """Build comparable facts from a stored row plus its decrypted values."""

    return ObservationFacts(
        observation_id=observation.pk,
        user_id=observation.user_id,
        occurred_at=observation.occurred_at,
        amount_minor=amount_minor,
        currency=observation.currency,
        direction=observation.direction,
        merchant=merchant,
        approval_code=approval_code,
        balance_after_minor=balance_after_minor,
        instrument_id=observation.payment_instrument_guess_id,
        account_id=observation.financial_account_guess_id,
        source_type=source_type,
        source_document_id=observation.source_document_id,
    )
