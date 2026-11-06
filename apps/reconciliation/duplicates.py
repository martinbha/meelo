"""Finding the same transaction imported twice.

Two mechanisms, deliberately separate:

* A **deterministic key** — same user, mapped instrument or account, amount,
  date, currency, and direction — finds the strongest cases cheaply. Approval
  codes support a score but never stand in for the row identity.
* A **score** (specification 16.3) ranks the uncertain ones so a reviewer sees
  the most likely pairs first.

Candidate generation never merges on its own. The optional automatic merge
policy lives in the persistence service, where it rechecks locked observations
against hard safety rules before changing either row.
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

#: Candidate generation bounds. A complete row-key match is prioritized, while
#: all candidate pairs remain inside the date window.
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

    Approval codes are only supporting evidence: issuers can reuse them, and a
    code alone is not unique across dates or cards. The key uses the complete
    row identity instead: one user's mapped instrument or account, date, amount,
    currency, and direction. An approval code still contributes to the score
    for candidates whose other facts agree.

    The search key is required, not optional. Dates, amounts, and account
    identifiers are low entropy, so an unkeyed digest would be a lookup table an
    attacker could build in seconds. Leaving an unkeyed path available would
    mean one caller could reach it, and the value would then be indistinguishable
    from a keyed one at the point where it mattered (specification 22.4).
    """

    if facts.amount_minor is None or facts.occurred_at is None or not facts.currency:
        return ""
    if facts.instrument_id is not None:
        mapping = f"instrument:{facts.instrument_id}"
    elif facts.account_id is not None:
        mapping = f"account:{facts.account_id}"
    else:
        mapping = "unmapped"
    payload = "|".join(
        (
            "row",
            str(facts.user_id),
            mapping,
            facts.occurred_at.isoformat(),
            str(facts.amount_minor),
            facts.currency.strip().upper(),
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


def _mapping_conflict(left: ObservationFacts, right: ObservationFacts) -> str:
    """Whether both rows explicitly map to different cards or accounts."""

    if (
        left.instrument_id is not None
        and right.instrument_id is not None
        and left.instrument_id != right.instrument_id
    ):
        return "different payment instruments"
    if (
        left.account_id is not None
        and right.account_id is not None
        and left.account_id != right.account_id
    ):
        return "different financial accounts"
    return ""


def _pair_conflict(left: ObservationFacts, right: ObservationFacts) -> str:
    """Hard incompatibilities that weighted evidence cannot override."""

    if left.user_id != right.user_id:
        return "different users"
    if left.observation_id == right.observation_id:
        return "same observation"

    left_currency = left.currency.strip().upper()
    right_currency = right.currency.strip().upper()
    if left_currency and right_currency and left_currency != right_currency:
        return "different currencies"
    if (
        left.amount_minor is not None
        and right.amount_minor is not None
        and left.amount_minor != right.amount_minor
    ):
        return "different amounts"
    if (
        left.direction != ImportedObservation.Direction.UNKNOWN
        and right.direction != ImportedObservation.Direction.UNKNOWN
        and left.direction != right.direction
    ):
        return "different directions"
    return _mapping_conflict(left, right)


def automatic_merge_blocking_reason(left: ObservationFacts, right: ObservationFacts) -> str:
    """Why a pair may not be merged automatically, regardless of its score.

    A strong score is not a substitute for a dated, mapped, same-currency event
    with a known direction. Two distinct source documents are required as well:
    two identical lines in one screenshot can be two real repeated purchases.
    """

    conflict = _pair_conflict(left, right)
    if conflict:
        return conflict
    if left.amount_minor is None or right.amount_minor is None:
        return "amount is missing"
    if not left.currency.strip() or not right.currency.strip():
        return "currency is missing"
    if left.occurred_at is None or right.occurred_at is None:
        return "date is missing"
    if abs(left.occurred_at - right.occurred_at) > NEARBY_DATE_WINDOW:
        return "dates are more than one day apart"
    if (
        left.direction == ImportedObservation.Direction.UNKNOWN
        or right.direction == ImportedObservation.Direction.UNKNOWN
    ):
        return "direction is unknown"
    if not (
        left.instrument_id is not None
        and left.instrument_id == right.instrument_id
        or left.account_id is not None
        and left.account_id == right.account_id
    ):
        return "no matching card or account mapping"
    if (
        left.approval_code
        and right.approval_code
        and left.approval_code.strip().casefold() != right.approval_code.strip().casefold()
    ):
        return "approval codes differ"
    if (
        left.source_document_id is None
        or right.source_document_id is None
        or left.source_document_id == right.source_document_id
    ):
        return "rows do not come from distinct source documents"
    return ""


def safe_for_automatic_merge(left: ObservationFacts, right: ObservationFacts) -> bool:
    """Whether the pair has every hard fact required for automatic merging."""

    return not automatic_merge_blocking_reason(left, right)


def score_pair(left: ObservationFacts, right: ObservationFacts) -> DuplicateScore:
    """Score how likely two observations are to be the same transaction."""

    blocking_reason = _pair_conflict(left, right)
    if blocking_reason:
        return DuplicateScore(0, (), blocking_reason)

    score = 0
    features: list[str] = []

    if (
        left.amount_minor is not None
        and left.amount_minor == right.amount_minor
        and left.currency.strip().upper() == right.currency.strip().upper()
    ):
        score += EXACT_AMOUNT_POINTS
        features.append("exact_amount")

    same_instrument = left.instrument_id is not None and left.instrument_id == right.instrument_id
    same_account = left.account_id is not None and left.account_id == right.account_id
    if same_instrument or same_account:
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

    if (
        left.approval_code
        and left.approval_code.strip().casefold() == right.approval_code.strip().casefold()
        and left.occurred_at is not None
        and right.occurred_at is not None
        and abs(left.occurred_at - right.occurred_at) <= NEARBY_DATE_WINDOW
    ):
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

    A pair reached through the complete row key is returned even when a caller
    raises the minimum score: each identity component already agrees. Approval
    codes alone do not bypass either the score threshold or the date window.
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
            if score.blocking_reason:
                continue
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
