"""What is still waiting for a decision, and where to go and make it.

The reports in this app answer "what did I spend". This one answers the question
that has to come first: **is what I am looking at complete?** A month's total is
only as trustworthy as the pile of unreviewed screenshots behind it, and a user
who cannot see that pile has no way to tell a small month from an unfinished one.

So every count here is a link. A number a user cannot act on tells them they have
work without telling them where it is, which is worse than not showing it —
they now know the total is incomplete and still cannot fix it.

Two things this must not do:

- **Count another user's work.** Every query is owner-scoped, and the document
  list carries identifiers that are only ever the requesting user's.
- **Let rejected rows near a total.** A rejected observation is a candidate the
  user threw away. It stays visible here, as a decision they made, and it never
  reaches a confirmed figure — reports read canonical transactions, and a
  rejected row has none (specification 19, 25.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q

from apps.core.ownership import owned_queryset
from apps.observations.models import ImportedObservation
from apps.observations.queue import QueueFilter, queue_counts
from apps.observations.risk import HIGH_RISK_THRESHOLD, LOW_CONFIDENCE_THRESHOLD
from apps.processing.models import SourceDocument
from apps.reconciliation.models import ReconciliationMatch

#: Confidence above which a parse is treated as trustworthy without a second look.
HIGH_CONFIDENCE_THRESHOLD = 0.9


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One count, and the place a user goes to work through it."""

    key: str
    label: str
    count: int
    #: Query string for the review queue, or empty when the link is elsewhere.
    query: str = ""
    #: Named URL this count links to.
    url_name: str = "review-queue"
    note: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.count > 0


@dataclass(frozen=True, slots=True)
class DocumentBacklog:
    """How much of one screenshot is still undecided."""

    document_id: Any
    source_type: str
    uploaded_at: Any
    total: int
    unreviewed: int
    accepted: int
    rejected: int
    merged: int

    @property
    def is_complete(self) -> bool:
        return self.unreviewed == 0

    @property
    def decided(self) -> int:
        return self.accepted + self.rejected + self.merged


@dataclass(frozen=True, slots=True)
class Workload:
    """Everything outstanding, grouped the four ways a user asks about it."""

    review_statuses: tuple[WorkItem, ...]
    confidence_bands: tuple[WorkItem, ...]
    queue_filters: tuple[WorkItem, ...]
    reconciliation: tuple[WorkItem, ...]
    documents: tuple[DocumentBacklog, ...]

    @property
    def unreviewed_count(self) -> int:
        return next((item.count for item in self.review_statuses if item.key == "unreviewed"), 0)

    @property
    def open_match_count(self) -> int:
        return next((item.count for item in self.reconciliation if item.key == "proposed"), 0)

    @property
    def is_clear(self) -> bool:
        """Whether there is nothing left to decide."""

        return self.unreviewed_count == 0 and self.open_match_count == 0

    @property
    def incomplete_documents(self) -> tuple[DocumentBacklog, ...]:
        return tuple(item for item in self.documents if not item.is_complete)


_STATUS_LABELS: Mapping[str, str] = {
    ImportedObservation.ReviewStatus.UNREVIEWED: "Waiting for review",
    ImportedObservation.ReviewStatus.ACCEPTED: "Accepted",
    ImportedObservation.ReviewStatus.CORRECTED: "Corrected and accepted",
    ImportedObservation.ReviewStatus.REJECTED: "Rejected",
    ImportedObservation.ReviewStatus.MERGED: "Merged into another row",
}

_MATCH_STATUS_LABELS: Mapping[str, str] = {
    ReconciliationMatch.Status.PROPOSED: "Waiting for a decision",
    ReconciliationMatch.Status.CONFIRMED: "Confirmed",
    ReconciliationMatch.Status.REJECTED: "Dismissed",
}

_QUEUE_FILTER_LABELS: Mapping[str, str] = {
    QueueFilter.LOW_CONFIDENCE: "Low confidence",
    QueueFilter.AMOUNT_DISAGREEMENT: "Amount in doubt",
    QueueFilter.UNKNOWN_MAPPING: "No account or card",
    QueueFilter.DUPLICATE: "Possible duplicate",
    QueueFilter.TRANSFER: "Possible transfer",
    QueueFilter.SETTLEMENT: "Possible settlement",
    QueueFilter.REFUND: "Possible refund",
    QueueFilter.MISSING_FIELDS: "Missing fields",
    QueueFilter.BALANCE_MISMATCH: "Balance chain broken",
}


def _review_statuses(user: Any) -> tuple[WorkItem, ...]:
    counts = dict(
        owned_queryset(ImportedObservation, user)
        .values_list("review_status")
        .annotate(total=Count("pk"))
    )
    return tuple(
        WorkItem(
            key=str(status),
            label=label,
            count=counts.get(status, 0),
            # No filter: the review queue already shows exactly the rows still
            # waiting, so the unfiltered link is the right destination and the
            # decided statuses have nowhere more specific to go.
            note=(
                "Kept as a decision you made. Never counted in a total."
                if status == ImportedObservation.ReviewStatus.REJECTED
                else ""
            ),
        )
        for status, label in _STATUS_LABELS.items()
    )


def _confidence_bands(user: Any) -> tuple[WorkItem, ...]:
    """How trustworthy the parses waiting for review are.

    Bands rather than an average: one row parsed at 0.2 among fifty at 0.98 is
    the row that matters, and an average of 0.96 hides it.
    """

    open_rows = owned_queryset(ImportedObservation, user).filter(
        review_status=ImportedObservation.ReviewStatus.UNREVIEWED
    )
    aggregated = open_rows.aggregate(
        high=Count("pk", filter=Q(overall_confidence__gte=HIGH_CONFIDENCE_THRESHOLD)),
        medium=Count(
            "pk",
            filter=Q(
                overall_confidence__gt=LOW_CONFIDENCE_THRESHOLD,
                overall_confidence__lt=HIGH_CONFIDENCE_THRESHOLD,
            ),
        ),
        low=Count("pk", filter=Q(overall_confidence__lte=LOW_CONFIDENCE_THRESHOLD)),
        risky=Count("pk", filter=Q(risk_score__gte=HIGH_RISK_THRESHOLD)),
    )
    return (
        WorkItem(
            "high", f"Confident ({HIGH_CONFIDENCE_THRESHOLD:.0%} and above)", aggregated["high"]
        ),
        WorkItem("medium", "Uncertain", aggregated["medium"]),
        WorkItem(
            "low",
            f"Low confidence ({LOW_CONFIDENCE_THRESHOLD:.0%} or below)",
            aggregated["low"],
            query=f"filter={QueueFilter.LOW_CONFIDENCE.value}",
        ),
        WorkItem(
            "risky",
            "High risk",
            aggregated["risky"],
            note="Refuses acceptance without explicit confirmation.",
        ),
    )


def _queue_filters(user: Any, *, match_ids: Mapping[str, Sequence[Any]]) -> tuple[WorkItem, ...]:
    """Counts per review-queue filter, taken from the queue itself.

    Read through :func:`apps.observations.queue.queue_counts` rather than
    recomputed here, so this page and the queue can never disagree about how
    many rows are waiting.
    """

    counts = queue_counts(user, match_ids=match_ids)
    return tuple(
        WorkItem(
            key=name.value,
            label=_QUEUE_FILTER_LABELS[name],
            count=counts.get(name.value, 0),
            query=f"filter={name.value}",
        )
        for name in QueueFilter
    )


def _reconciliation(user: Any) -> tuple[WorkItem, ...]:
    counts = dict(
        owned_queryset(ReconciliationMatch, user).values_list("status").annotate(total=Count("pk"))
    )
    return tuple(
        WorkItem(
            key=str(status),
            label=label,
            count=counts.get(status, 0),
            url_name="match-queue",
            note=(
                "Dismissed pairings are not proposed again."
                if status == ReconciliationMatch.Status.REJECTED
                else ""
            ),
        )
        for status, label in _MATCH_STATUS_LABELS.items()
    )


def _documents(user: Any, *, limit: int = 25) -> tuple[DocumentBacklog, ...]:
    """Per-screenshot progress, incomplete ones first."""

    documents = (
        owned_queryset(SourceDocument, user)
        .annotate(
            total=Count("imported_observations"),
            unreviewed=Count(
                "imported_observations",
                filter=Q(
                    imported_observations__review_status=(
                        ImportedObservation.ReviewStatus.UNREVIEWED
                    )
                ),
            ),
            accepted=Count(
                "imported_observations",
                filter=Q(
                    imported_observations__review_status__in=[
                        ImportedObservation.ReviewStatus.ACCEPTED,
                        ImportedObservation.ReviewStatus.CORRECTED,
                    ]
                ),
            ),
            rejected=Count(
                "imported_observations",
                filter=Q(
                    imported_observations__review_status=(ImportedObservation.ReviewStatus.REJECTED)
                ),
            ),
            merged=Count(
                "imported_observations",
                filter=Q(
                    imported_observations__review_status=ImportedObservation.ReviewStatus.MERGED
                ),
            ),
        )
        .filter(total__gt=0)
        .order_by("-unreviewed", "-uploaded_at")[:limit]
    )
    return tuple(
        DocumentBacklog(
            document_id=document.pk,
            source_type=document.source_type,
            uploaded_at=document.uploaded_at,
            total=document.total,
            unreviewed=document.unreviewed,
            accepted=document.accepted,
            rejected=document.rejected,
            merged=document.merged,
        )
        for document in documents
    )


def outstanding_work(
    user: Any, *, match_ids: Mapping[str, Sequence[Any]] | None = None
) -> Workload:
    """Everything this user still has to decide."""

    return Workload(
        review_statuses=_review_statuses(user),
        confidence_bands=_confidence_bands(user),
        queue_filters=_queue_filters(user, match_ids=dict(match_ids or {})),
        reconciliation=_reconciliation(user),
        documents=_documents(user),
    )
