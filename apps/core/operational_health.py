"""Privacy-safe worker and queue readings shared by health surfaces."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.processing.models import ProcessingJob, SourceDocument


def worker_queue_summary() -> dict[str, int | float]:
    """Return counts and ages only, never payloads or owner identifiers."""

    now = timezone.now()
    queued = ProcessingJob.objects.filter(status=ProcessingJob.Status.QUEUED)
    oldest = queued.order_by("created_at").values_list("created_at", flat=True).first()
    oldest_age = -1.0 if oldest is None else max(0.0, (now - oldest).total_seconds())

    active_statuses = (
        SourceDocument.Status.VALIDATING,
        SourceDocument.Status.PREPROCESSING,
        SourceDocument.Status.OCR_RUNNING,
        SourceDocument.Status.PARSING,
    )
    stuck_cutoff = now - timedelta(seconds=settings.PROCESSING_STUCK_AFTER_SECONDS)
    stuck = SourceDocument.objects.filter(processing_status__in=active_statuses).filter(
        Q(processing_started_at__isnull=True) | Q(processing_started_at__lte=stuck_cutoff)
    )

    from apps.core.models import WorkerHeartbeat

    heartbeat = WorkerHeartbeat.objects.order_by("-last_seen_at").first()
    heartbeat_age = (
        -1.0 if heartbeat is None else max(0.0, (now - heartbeat.last_seen_at).total_seconds())
    )
    worker_available = int(
        heartbeat_age >= 0 and heartbeat_age <= settings.WORKER_HEARTBEAT_STALE_SECONDS
    )
    return {
        "queue_depth": queued.count(),
        "oldest_queued_age_seconds": round(oldest_age, 3),
        "stuck_documents": stuck.count(),
        "worker_heartbeat_age_seconds": round(heartbeat_age, 3),
        "worker_available": worker_available,
    }
