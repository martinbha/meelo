from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import InvalidRequestError

from .cleanup import cleanup_document_storage
from .models import SourceDocument
from .state import transition_document

RETENTION_DAYS: dict[str, int] = {
    SourceDocument.RetentionPolicy.IMMEDIATE: 0,
    SourceDocument.RetentionPolicy.ONE_DAY: 1,
    SourceDocument.RetentionPolicy.SEVEN_DAYS: 7,
    SourceDocument.RetentionPolicy.THIRTY_DAYS: 30,
}


def retention_deadline(policy: str, *, uploaded_at: Any | None = None) -> Any:
    if policy not in RETENTION_DAYS:
        raise InvalidRequestError("Unsupported screenshot retention policy.")
    start = uploaded_at or timezone.now()
    return start + timedelta(days=RETENTION_DAYS[policy])


@transaction.atomic
def delete_document(document_id: Any, *, user: Any) -> SourceDocument:
    document = SourceDocument.objects.select_for_update().filter(pk=document_id, user=user).first()
    if document is None:
        raise InvalidRequestError("Document not found.")
    if document.processing_status != SourceDocument.Status.DELETED:
        cleanup_document_storage(document.pk, document.temporary_path)
        document = transition_document(document.pk, user=user, status=SourceDocument.Status.DELETED)
        record_audit_event(
            user=user,
            event_type="screenshot_deleted",
            obj=document,
            metadata={"retention_policy": document.retention_policy},
        )
    return document


def expire_documents(*, now: Any | None = None) -> int:
    cutoff = now or timezone.now()
    documents = SourceDocument.objects.filter(
        retention_deadline__lte=cutoff,
        processing_status__in=[
            SourceDocument.Status.READY_FOR_REVIEW,
            SourceDocument.Status.CONFIRMED,
            SourceDocument.Status.FAILED,
        ],
    )
    count = 0
    for document in documents.iterator():
        delete_document(document.pk, user=document.user)
        count += 1
    return count
