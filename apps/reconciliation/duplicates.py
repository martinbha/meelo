"""Finding the same transaction imported twice.

Two mechanisms, deliberately separate:

* A **deterministic key** — same user, instrument, amount, date, direction, or
  an identical approval code — finds the certain cases cheaply.
* A **score** (specification 16.3) ranks the uncertain ones so a reviewer sees
  the most likely pairs first.

Neither ever deletes or merges anything on its own. Silent automatic merging is
disabled for the initial release, so both paths only ever produce candidates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rapidfuzz.fuzz import ratio

from apps.core import metrics
from apps.core.blind_index import SearchKey, blind_index
from apps.observations.models import ImportedObservation

#: Feature weights from specification 16.3.
EXACT_AMOUNT_POINTS = 30
SAME_INSTRUMENT_POINTS = 25
NEARBY_DATE_POINTS = 15
SAME_DIRECTION_POINTS = 10
MERCHANT_SIMILARITY_POINTS = 10
SAME_APPROVAL_CODE_POINTS = 30
SAME_BALANCE_POINTS = 15
SAME_SOURCE_TYPE_POINTS = 5

#: Score thresholds from specification 16.3.
LIKELY_MERGE_SCORE = 90
REVIEW_CANDIDATE_SCORE = 65

#: How far apart two dates may be and still count as the same event.
NEARBY_DATE_WINDOW = timedelta(days=1)
#: Merchant strings at or above this similarity count as the same merchant.
MERCHANT_SIMILARITY_THRESHOLD = 85.0

#: Candidate generation bounds. Exact blind-index pairs bypass the date window,
#: but still compete by score for the finite reviewer-facing candidate set.
CANDIDATE_SEARCH_WINDOW = timedelta(days=31)
MAX_CANDIDATES_PER_OBSERVATION = 25


@dataclass(frozen=True, slots=True)
class ObservationFacts:
    """The comparable facts of one observation, already decrypted.

    Decryption happens once in the caller so scoring never needs the data key
    and can be reasoned about — and tested — as pure arithmetic.
    """

    observation_id: Any
    user_id: Any
    occurred_at: date | None
    amount_minor: int | None
    currency: str
    direction: str
    merchant: str = ""
    approval_code: str = ""
    balance_after_minor: int | None = None
    instrument_id: Any = None
    account_id: Any = None
    source_type: str = ""
    source_document_id: Any = None


@dataclass(frozen=True, slots=True)
class DuplicateScore:
    """A score plus the features that produced it."""

    score: int
    features: tuple[str, ...] = ()
    blocking_reason: str = ""

    @property
    def is_likely_merge(self) -> bool:
        return self.score >= LIKELY_MERGE_SCORE

    @property
    def is_review_candidate(self) -> bool:
        return REVIEW_CANDIDATE_SCORE <= self.score < LIKELY_MERGE_SCORE

    @property
    def keeps_separate(self) -> bool:
        return self.score < REVIEW_CANDIDATE_SCORE

    @property
    def strength(self) -> str:
        if self.is_likely_merge:
            return "likely_merge"
        if self.is_review_candidate:
            return "review_candidate"
        return "weak"

    def as_features(self) -> Mapping[str, Any]:
        return {"score": self.score, "matched": list(self.features)}


def deterministic_key(facts: ObservationFacts, *, search_key: SearchKey | bytes) -> str:
    """A stable key for the rows that can be matched without judgement.

    An approval code identifies one authorisation exactly, so it takes
    precedence. Otherwise the key is the tuple a bank statement would use to
    identify a line: instrument, date, signed amount.

    The search key is required, not optional. Everything this covers is low
    entropy — a six-digit approval code, a date, an amount a coffee might cost —
    so an unkeyed digest of it is a lookup table an attacker can build in
    seconds. Leaving an unkeyed path available would mean one caller could reach
    it, and the value would then be indistinguishable from a keyed one at the
    point where it mattered (specification 22.4).
    """

    if facts.approval_code:
        payload = f"approval|{facts.user_id}|{facts.approval_code.strip().casefold()}"
    else:
        if facts.amount_minor is None or facts.occurred_at is None:
            return ""
        payload = "|".join(
            (
                "row",
                str(facts.user_id),
                str(facts.instrument_id or facts.account_id or "unmapped"),
                facts.occurred_at.isoformat(),
                str(facts.amount_minor),
                facts.currency,
                facts.direction,
            )
        )
    return blind_index("observation_row", payload, user_id=facts.user_id, key=search_key)


def group_by_key(
    facts: Iterable[ObservationFacts], *, search_key: SearchKey | bytes
) -> dict[str, list[ObservationFacts]]:
    """Group observations that share a deterministic key."""

    grouped: dict[str, list[ObservationFacts]] = {}
    for item in facts:
        key = deterministic_key(item, search_key=search_key)
        if not key:
            continue
        grouped.setdefault(key, []).append(item)
    return {key: items for key, items in grouped.items() if len(items) > 1}


def _merchant_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return float(ratio(left.casefold(), right.casefold()))


def score_pair(left: ObservationFacts, right: ObservationFacts) -> DuplicateScore:
    """Score how likely two observations are to be the same transaction."""

    if left.user_id != right.user_id:
        # Same user is a hard requirement, not a weighted feature.
        return DuplicateScore(0, (), "different users")
    if left.observation_id == right.observation_id:
        return DuplicateScore(0, (), "same observation")

    score = 0
    features: list[str] = []

    if (
        left.amount_minor is not None
        and left.amount_minor == right.amount_minor
        and left.currency == right.currency
    ):
        score += EXACT_AMOUNT_POINTS
        features.append("exact_amount")

    left_instrument = left.instrument_id or left.account_id
    right_instrument = right.instrument_id or right.account_id
    if left_instrument is not None and left_instrument == right_instrument:
        score += SAME_INSTRUMENT_POINTS
        features.append("same_mapped_instrument")

    if (
        left.occurred_at is not None
        and right.occurred_at is not None
        and abs(left.occurred_at - right.occurred_at) <= NEARBY_DATE_WINDOW
    ):
        score += NEARBY_DATE_POINTS
        features.append("date_within_one_day")

    if (
        left.direction == right.direction
        and left.direction != ImportedObservation.Direction.UNKNOWN
    ):
        score += SAME_DIRECTION_POINTS
        features.append("same_direction")

    if _merchant_similarity(left.merchant, right.merchant) >= MERCHANT_SIMILARITY_THRESHOLD:
        score += MERCHANT_SIMILARITY_POINTS
        features.append("merchant_similarity")

    if left.approval_code and left.approval_code == right.approval_code:
        score += SAME_APPROVAL_CODE_POINTS
        features.append("same_approval_code")

    if (
        left.balance_after_minor is not None
        and left.balance_after_minor == right.balance_after_minor
    ):
        score += SAME_BALANCE_POINTS
        features.append("same_balance_after")

    if left.source_type and left.source_type == right.source_type:
        score += SAME_SOURCE_TYPE_POINTS
        features.append("same_source_type")

    return DuplicateScore(min(100, score), tuple(features))


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """One scored pair, ready to be stored as a candidate."""

    left: ObservationFacts
    right: ObservationFacts
    score: DuplicateScore
    from_deterministic_key: bool = False
    features: tuple[str, ...] = field(default_factory=tuple)


def find_duplicate_candidates(
    facts: Sequence[ObservationFacts],
    *,
    search_key: SearchKey | bytes,
    minimum_score: int = REVIEW_CANDIDATE_SCORE,
    search_window: timedelta = CANDIDATE_SEARCH_WINDOW,
    max_candidates_per_observation: int = MAX_CANDIDATES_PER_OBSERVATION,
) -> tuple[DuplicateCandidate, ...]:
    """Pair up observations that may be the same transaction.

    A pair reached through a deterministic key is always returned, whatever it
    scores: an identical approval code is evidence no weighting should be able
    to talk us out of.
    """

    if search_window < timedelta(0):
        raise ValueError("The candidate search window cannot be negative.")
    if max_candidates_per_observation < 1:
        raise ValueError("The per-observation candidate cap must be positive.")

    keyed_pairs: set[tuple[Any, Any]] = set()
    for group in group_by_key(facts, search_key=search_key).values():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                keyed_pairs.add((left.observation_id, right.observation_id))

    candidates: list[DuplicateCandidate] = []
    for index, left in enumerate(facts):
        for right in facts[index + 1 :]:
            score = score_pair(left, right)
            deterministic = (left.observation_id, right.observation_id) in keyed_pairs
            if (
                not deterministic
                and left.occurred_at is not None
                and right.occurred_at is not None
                and abs(left.occurred_at - right.occurred_at) > search_window
            ):
                continue
            if not deterministic and score.score < minimum_score:
                continue
            candidates.append(
                DuplicateCandidate(
                    left=left,
                    right=right,
                    score=score,
                    from_deterministic_key=deterministic,
                    features=score.features,
                )
            )
    candidates.sort(
        key=lambda item: (
            not item.from_deterministic_key,
            -item.score.score,
            str(item.left.observation_id),
        )
    )
    selected: list[DuplicateCandidate] = []
    counts: dict[Any, int] = {}
    capped = False
    for candidate in candidates:
        left_id = candidate.left.observation_id
        right_id = candidate.right.observation_id
        if (
            counts.get(left_id, 0) >= max_candidates_per_observation
            or counts.get(right_id, 0) >= max_candidates_per_observation
        ):
            capped = True
            continue
        selected.append(candidate)
        counts[left_id] = counts.get(left_id, 0) + 1
        counts[right_id] = counts.get(right_id, 0) + 1
    if capped:
        metrics.record(
            metrics.MATCH_PROPOSED,
            status="capped",
            match_type="duplicate_observation",
        )
    return tuple(selected)
