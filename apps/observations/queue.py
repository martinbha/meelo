"""The review queue: what a reviewer should look at, hardest cases first.

Every imported observation stays in the queue until it is accepted, corrected,
rejected, or merged. Nothing leaves it silently, because a row that quietly
disappeared would be a transaction the user never agreed to.

Ordering is by risk, and the ordering happens in the database. A high-risk row
must sort ahead of routine work across the whole queue, not merely within
whichever page it landed on, so the score lives on the row (see
:mod:`apps.observations.risk`) rather than being computed after pagination.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, QuerySet, Value, When

from apps.core.ownership import owned_queryset

from .models import ImportedObservation
from .risk import HIGH_RISK_THRESHOLD, LOW_CONFIDENCE_THRESHOLD, score_flags

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


class QueueFilter(StrEnum):
    """The filters the review queue offers."""

    LOW_CONFIDENCE = "low_confidence"
    AMOUNT_DISAGREEMENT = "amount_disagreement"
    UNKNOWN_MAPPING = "unknown_mapping"
    DUPLICATE = "duplicate"
    TRANSFER = "transfer"
    SETTLEMENT = "settlement"
    REFUND = "refund"
    MISSING_FIELDS = "missing_fields"
    BALANCE_MISMATCH = "balance_mismatch"


#: Filters whose membership comes from the reconciliation layer rather than
#: from the observation itself. The caller supplies the matching identifiers so
#: this module never imports reconciliation, which already depends on it.
MATCH_DERIVED_FILTERS: tuple[QueueFilter, ...] = (
    QueueFilter.DUPLICATE,
    QueueFilter.TRANSFER,
    QueueFilter.SETTLEMENT,
    QueueFilter.REFUND,
)

#: Risk contributed by a reconciliation candidate attached to a row.
MATCH_RISK: Mapping[QueueFilter, int] = {
    QueueFilter.DUPLICATE: 88,
    QueueFilter.SETTLEMENT: 60,
    QueueFilter.TRANSFER: 55,
    QueueFilter.REFUND: 50,
}


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One queue row together with why it is where it is."""

    observation: ImportedObservation
    risk_score: int
    reasons: tuple[str, ...]

    @property
    def is_high_risk(self) -> bool:
        return self.risk_score >= HIGH_RISK_THRESHOLD


@dataclass(frozen=True, slots=True)
class QueuePage:
    """A page of queue items plus the counts a reviewer needs to plan."""

    items: tuple[QueueItem, ...]
    page_number: int
    page_count: int
    total: int
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def has_next(self) -> bool:
        return self.page_number < self.page_count

    @property
    def has_previous(self) -> bool:
        return self.page_number > 1


def open_observations(user: Any) -> QuerySet[ImportedObservation]:
    """Every observation still awaiting a decision, scoped to its owner."""

    return owned_queryset(ImportedObservation, user).filter(
        review_status=ImportedObservation.ReviewStatus.UNREVIEWED
    )


def _match_pks(match_ids: Mapping[str, Sequence[Any]], name: QueueFilter) -> list[Any]:
    return list(match_ids.get(name.value, ()))


def _apply_filter(
    queryset: QuerySet[ImportedObservation],
    name: QueueFilter,
    *,
    match_ids: Mapping[str, Sequence[Any]],
) -> QuerySet[ImportedObservation]:
    if name in MATCH_DERIVED_FILTERS:
        return queryset.filter(pk__in=_match_pks(match_ids, name))
    if name is QueueFilter.LOW_CONFIDENCE:
        return queryset.filter(overall_confidence__lte=LOW_CONFIDENCE_THRESHOLD)
    if name is QueueFilter.AMOUNT_DISAGREEMENT:
        return queryset.filter(amount_uncertain=True)
    if name is QueueFilter.UNKNOWN_MAPPING:
        return queryset.filter(
            financial_account_guess__isnull=True, payment_instrument_guess__isnull=True
        )
    if name is QueueFilter.BALANCE_MISMATCH:
        return queryset.filter(balance_mismatched=True)
    if name is QueueFilter.MISSING_FIELDS:
        return queryset.filter(has_missing_fields=True)
    return queryset


def _explain(
    observation: ImportedObservation, match_filters: Sequence[QueueFilter]
) -> tuple[int, tuple[str, ...]]:
    """Recompute the score for display, including reconciliation candidates.

    The stored score already covers everything intrinsic to the row; a pending
    duplicate or settlement candidate can only raise it.
    """

    has_mapping = (
        observation.financial_account_guess_id is not None
        or observation.payment_instrument_guess_id is not None
    )
    score, reasons = score_flags(
        [str(flag) for flag in observation.review_flags or ()],
        overall_confidence=observation.overall_confidence,
        has_mapping=has_mapping,
    )
    extra = [
        (MATCH_RISK[name], f"{name.value}_candidate")
        for name in match_filters
        if name in MATCH_RISK
    ]
    if extra:
        score = max(score, max(value for value, _ in extra))
        reasons = tuple(
            reason
            for _, reason in sorted(
                [*extra, *((0, name) for name in reasons)], key=lambda item: -item[0]
            )
        )
    return score, reasons


def queue_counts(
    user: Any, *, match_ids: Mapping[str, Sequence[Any]] | None = None
) -> dict[str, int]:
    """Counts per filter for one user, and never across users.

    Ownership scoping happens in :func:`open_observations`, so a count can only
    ever describe rows the requesting user owns.
    """

    resolved = dict(match_ids or {})
    base = open_observations(user)
    counts = {"open": base.count()}
    for name in QueueFilter:
        counts[name.value] = _apply_filter(base, name, match_ids=resolved).count()
    return counts


def review_queue(
    user: Any,
    *,
    filters: Iterable[QueueFilter | str] = (),
    match_ids: Mapping[str, Sequence[Any]] | None = None,
    page_number: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> QueuePage:
    """Build one page of prioritized review work for ``user``."""

    resolved = dict(match_ids or {})
    queryset = open_observations(user).select_related(
        "source_document", "financial_account_guess", "payment_instrument_guess", "category_guess"
    )
    for name in filters:
        queryset = _apply_filter(queryset, QueueFilter(name), match_ids=resolved)

    # A pending reconciliation candidate raises a row's effective risk, so it
    # has to participate in the database ordering rather than only in display.
    match_cases = [
        When(pk__in=_match_pks(resolved, name), then=Value(MATCH_RISK[name]))
        for name in MATCH_DERIVED_FILTERS
        if _match_pks(resolved, name)
    ]
    match_risk = (
        Case(*match_cases, default=Value(0), output_field=IntegerField())
        if match_cases
        else Value(0, output_field=IntegerField())
    )
    queryset = queryset.annotate(match_risk=match_risk).order_by(
        "-risk_score", "-match_risk", "overall_confidence", "occurred_at", "row_index", "pk"
    )

    size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    paginator = Paginator(queryset, size)
    page = paginator.get_page(page_number)
    items = []
    for observation in page.object_list:
        matched = tuple(
            name
            for name in MATCH_DERIVED_FILTERS
            if observation.pk in set(_match_pks(resolved, name))
        )
        score, reasons = _explain(observation, matched)
        items.append(QueueItem(observation, score, reasons))

    return QueuePage(
        items=tuple(items),
        page_number=page.number,
        page_count=paginator.num_pages,
        total=paginator.count,
        counts=queue_counts(user, match_ids=resolved),
    )
