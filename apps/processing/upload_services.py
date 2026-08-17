from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.core import metrics
from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, InvalidRequestError

from .models import ProcessingJob, SourceDocument
from .retention import retention_deadline
from .storage import store_uploaded_file
from .validation import fingerprint_uploaded_file, validate_uploaded_file


class DuplicateUploadError(ConflictError):
    code = "DUPLICATE_UPLOAD"

    def __init__(self, document: SourceDocument) -> None:
        super().__init__(
            "This screenshot was already uploaded.", details={"document_id": str(document.pk)}
        )
        self.document = document


@transaction.atomic
def create_uploaded_document(
    *,
    user: Any,
    uploaded_file: Any,
    retention_policy: str = SourceDocument.RetentionPolicy.IMMEDIATE,
) -> SourceDocument:
    validated = validate_uploaded_file(uploaded_file)
    deadline = retention_deadline(retention_policy)
    content_type = validated.mime_type
    digest = fingerprint_uploaded_file(uploaded_file)
    existing = SourceDocument.objects.filter(user=user, file_sha256=digest).first()
    if existing is not None:
        raise DuplicateUploadError(existing)

    document_id = uuid.uuid4()
    path, stored_digest, size = store_uploaded_file(
        document_id, uploaded_file, suffix=validated.suffix
    )
    if size > settings.MAX_UPLOAD_SIZE:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        raise InvalidRequestError("The screenshot exceeds the upload size limit.")
    try:
        with transaction.atomic():
            document = SourceDocument.objects.create(
                id=document_id,
                user=user,
                file_sha256=stored_digest,
                original_filename_encrypted=str(uploaded_file.name),
                mime_type=content_type,
                file_size=size,
                image_width=validated.width,
                image_height=validated.height,
                temporary_path=str(path),
                processing_status=SourceDocument.Status.QUEUED,
                retention_policy=retention_policy,
                retention_deadline=deadline,
            )
            ProcessingJob.objects.create(
                user=user,
                document_id=document.id,
                task_name="process_document",
                payload={"document_id": str(document.id)},
            )
            record_audit_event(
                user=user,
                event_type="screenshot_uploaded",
                obj=document,
                metadata={"mime_type": content_type, "file_size": size},
            )
            metrics.record(metrics.UPLOAD_RECEIVED, document_id=str(document.id))
    except IntegrityError as exc:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        existing = SourceDocument.objects.filter(user=user, file_sha256=stored_digest).first()
        if existing is not None:
            metrics.record(metrics.UPLOAD_REJECTED, reason="duplicate")
            raise DuplicateUploadError(existing) from exc
        metrics.record(metrics.UPLOAD_REJECTED, reason="integrity_error")
        raise
    except Exception:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        raise
    return document
