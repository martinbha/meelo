"""Re-running OCR on a document a reviewer judged badly read.

Reprocessing is additive. Prior OCR runs and their observations are kept, a new
run produces new candidates alongside them, and confirmed transactions are never
touched — accepting a row and then reprocessing its document must not undo the
acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, ForbiddenError, InvalidRequestError
from apps.ocr.models import OcrRun
from apps.processing.models import SourceDocument

from .models import ImportedObservation

#: Statuses from which a reviewer may ask for another pass.
REPROCESSABLE_STATUSES = frozenset(
    {
        SourceDocument.Status.READY_FOR_REVIEW,
        SourceDocument.Status.FAILED,
        SourceDocument.Status.CONFIRMED,
    }
)

#: Statuses that mean a pass is already under way.
IN_FLIGHT_STATUSES = frozenset(
    {
        SourceDocument.Status.QUEUED,
        SourceDocument.Status.VALIDATING,
        SourceDocument.Status.PREPROCESSING,
        SourceDocument.Status.OCR_RUNNING,
        SourceDocument.Status.PARSING,
    }
)


class ReprocessError(InvalidRequestError):
    """The document cannot be reprocessed right now."""


@dataclass(frozen=True, slots=True)
class ReprocessRequest:
    """The outcome of asking for another OCR pass."""

    document: SourceDocument
    previous_status: str
    preserved_run_count: int
    preserved_observation_count: int


def latest_run(document: SourceDocument) -> OcrRun | None:
    """The most recent OCR run, which is the one review should present."""

    return OcrRun.objects.filter(source_document=document).order_by("-created_at", "-pk").first()


def is_latest_run(run: OcrRun) -> bool:
    current = latest_run(run.source_document)
    return current is not None and current.pk == run.pk


@db_transaction.atomic
def request_reprocess(document_id: Any, *, user: Any) -> ReprocessRequest:
    """Queue another OCR pass for a document, refusing concurrent reruns."""

    document = (
        SourceDocument.objects.select_for_update().filter(pk=document_id, user_id=user.pk).first()
    )
    if document is None:
        raise ForbiddenError("This document belongs to another user.")
    if document.processing_status in IN_FLIGHT_STATUSES:
        raise ConflictError("This document is already being processed.")
    if document.processing_status not in REPROCESSABLE_STATUSES:
        raise ReprocessError(
            f"A document in state '{document.processing_status}' cannot be reprocessed."
        )
    if document.original_deleted_at is not None:
        raise ReprocessError(
            "The original image was deleted under the retention policy, so it cannot be read again."
        )

    previous_status = document.processing_status
    preserved_runs = OcrRun.objects.filter(source_document=document).count()
    preserved_observations = ImportedObservation.objects.filter(source_document=document).count()

    document.processing_status = SourceDocument.Status.QUEUED
    document.error_code = ""
    document.next_processing_attempt_at = timezone.now()
    document.save(
        update_fields=[
            "processing_status",
            "error_code",
            "next_processing_attempt_at",
        ]
    )

    record_audit_event(
        user=user,
        event_type="document_reprocess_requested",
        obj=document,
        metadata={
            "previous_status": previous_status,
            "preserved_run_count": preserved_runs,
            "preserved_observation_count": preserved_observations,
        },
    )
    return ReprocessRequest(
        document=document,
        previous_status=previous_status,
        preserved_run_count=preserved_runs,
        preserved_observation_count=preserved_observations,
    )
