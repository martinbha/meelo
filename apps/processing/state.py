from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.errors import ConflictError, InvalidRequestError

from .models import ProcessingJob, SourceDocument

ALLOWED_DOCUMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    SourceDocument.Status.PENDING: frozenset(
        {SourceDocument.Status.VALIDATING, SourceDocument.Status.DELETED}
    ),
    SourceDocument.Status.VALIDATING: frozenset(
        {
            SourceDocument.Status.QUEUED,
            SourceDocument.Status.PREPROCESSING,
            SourceDocument.Status.FAILED,
        }
    ),
    SourceDocument.Status.QUEUED: frozenset(
        {SourceDocument.Status.VALIDATING, SourceDocument.Status.FAILED}
    ),
    SourceDocument.Status.PREPROCESSING: frozenset(
        {SourceDocument.Status.OCR_RUNNING, SourceDocument.Status.FAILED}
    ),
    SourceDocument.Status.OCR_RUNNING: frozenset(
        {SourceDocument.Status.PARSING, SourceDocument.Status.FAILED}
    ),
    SourceDocument.Status.PARSING: frozenset(
        {SourceDocument.Status.READY_FOR_REVIEW, SourceDocument.Status.FAILED}
    ),
    SourceDocument.Status.READY_FOR_REVIEW: frozenset(
        {SourceDocument.Status.CONFIRMED, SourceDocument.Status.FAILED}
    ),
    SourceDocument.Status.CONFIRMED: frozenset({SourceDocument.Status.DELETED}),
    SourceDocument.Status.FAILED: frozenset(
        {SourceDocument.Status.QUEUED, SourceDocument.Status.DELETED}
    ),
    SourceDocument.Status.DELETED: frozenset(),
}


@transaction.atomic
def transition_document(
    document_id: Any,
    *,
    user: Any,
    status: str,
    error_code: str = "",
    error_message: str = "",
) -> SourceDocument:
    document = SourceDocument.objects.select_for_update().filter(pk=document_id, user=user).first()
    if document is None:
        raise InvalidRequestError("Document not found.")
    if status not in ALLOWED_DOCUMENT_TRANSITIONS[document.processing_status]:
        raise ConflictError(
            f"Cannot change document status from {document.processing_status} to {status}."
        )

    now = timezone.now()
    document.processing_status = status
    if status == SourceDocument.Status.VALIDATING:
        document.processing_attempt_count += 1
        document.processing_started_at = now
        document.error_code = ""
        document.error_message_encrypted = ""
    elif status in {SourceDocument.Status.QUEUED, SourceDocument.Status.PREPROCESSING}:
        document.next_processing_attempt_at = (
            now if status == SourceDocument.Status.QUEUED else None
        )
    elif status in {
        SourceDocument.Status.READY_FOR_REVIEW,
        SourceDocument.Status.CONFIRMED,
        SourceDocument.Status.DELETED,
    }:
        document.processing_completed_at = now
        if status == SourceDocument.Status.DELETED:
            document.original_deleted_at = now
    elif status == SourceDocument.Status.FAILED:
        document.error_code = error_code[:64]
        document.error_message_encrypted = error_message
        document.processing_completed_at = now
        document.next_processing_attempt_at = None
    document.save(
        update_fields=[
            "processing_status",
            "processing_attempt_count",
            "processing_started_at",
            "processing_completed_at",
            "next_processing_attempt_at",
            "original_deleted_at",
            "error_code",
            "error_message_encrypted",
        ]
    )
    return document


def retry_failed_document(document_id: Any, *, user: Any) -> SourceDocument:
    document = transition_document(document_id, user=user, status=SourceDocument.Status.QUEUED)
    if not ProcessingJob.objects.filter(
        user=user, document_id=document.pk, status=ProcessingJob.Status.QUEUED
    ).exists():
        ProcessingJob.objects.create(
            user=user,
            document_id=document.pk,
            task_name="process_document",
            payload={"document_id": str(document.pk)},
        )
    return document
