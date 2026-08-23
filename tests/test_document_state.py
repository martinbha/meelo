from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from apps.core.errors import ConflictError
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.state import retry_failed_document, transition_document


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("state@example.com", password="password")


@pytest.fixture
def document(user: Any) -> SourceDocument:
    return SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="statement.png",
        mime_type="image/png",
        file_size=10,
        processing_status=SourceDocument.Status.QUEUED,
    )


@pytest.mark.django_db
def test_document_state_machine_records_progress_and_errors(
    user: Any, document: SourceDocument
) -> None:
    transition_document(document.pk, user=user, status=SourceDocument.Status.VALIDATING)
    transition_document(document.pk, user=user, status=SourceDocument.Status.PREPROCESSING)
    failed = transition_document(
        document.pk,
        user=user,
        status=SourceDocument.Status.FAILED,
        error_code="IMAGE_DECODE_FAILED",
        error_message="The image could not be decoded.",
    )

    assert failed.processing_attempt_count == 1
    assert failed.processing_completed_at is not None
    assert failed.error_code == "IMAGE_DECODE_FAILED"
    assert failed.error_message_encrypted == "The screenshot could not be read."
    with pytest.raises(ConflictError):
        transition_document(document.pk, user=user, status=SourceDocument.Status.CONFIRMED)


@pytest.mark.django_db
def test_failed_retry_requeues_one_job_without_duplicate_queued_work(
    user: Any, document: SourceDocument
) -> None:
    transition_document(document.pk, user=user, status=SourceDocument.Status.VALIDATING)
    transition_document(
        document.pk, user=user, status=SourceDocument.Status.FAILED, error_code="TEMPORARY"
    )
    ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
        status=ProcessingJob.Status.FAILED,
    )

    retried = retry_failed_document(document.pk, user=user)

    assert retried.processing_status == SourceDocument.Status.QUEUED
    assert (
        ProcessingJob.objects.filter(
            document_id=document.pk, status=ProcessingJob.Status.QUEUED
        ).count()
        == 1
    )
