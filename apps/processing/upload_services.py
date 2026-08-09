from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.core.audit import record_audit_event
from apps.core.errors import InvalidRequestError

from .models import ProcessingJob, SourceDocument
from .storage import store_uploaded_file
from .validation import validate_uploaded_file


@transaction.atomic
def create_uploaded_document(*, user: Any, uploaded_file: Any) -> SourceDocument:
    validated = validate_uploaded_file(uploaded_file)
    content_type = validated.mime_type

    document_id = uuid.uuid4()
    path, digest, size = store_uploaded_file(document_id, uploaded_file, suffix=validated.suffix)
    if size > settings.MAX_UPLOAD_SIZE:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        raise InvalidRequestError("The screenshot exceeds the upload size limit.")
    try:
        document = SourceDocument.objects.create(
            id=document_id,
            user=user,
            file_sha256=digest,
            original_filename_encrypted=str(uploaded_file.name),
            mime_type=content_type,
            file_size=size,
            image_width=validated.width,
            image_height=validated.height,
            temporary_path=str(path),
            processing_status=SourceDocument.Status.QUEUED,
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
    except Exception:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        raise
    return document
