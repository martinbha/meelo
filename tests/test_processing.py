import base64
import os
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.utils import timezone
from PIL import Image

from apps.core.context import request_id_context
from apps.core.key_management import provision_user_data_key
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.services import JOB_HANDLERS, process_one_job
from apps.processing.storage import document_directory


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def user(db: Any, master_key: bytes) -> Any:
    from django.contrib.auth import get_user_model

    # A data key, because the worker now opens one per document job — a worker
    # that cannot reach the owner's key cannot process their screenshot, and a
    # test that pretends otherwise is testing a pipeline that does not exist.
    account = get_user_model().objects.create_user("owner@example.com", password="password")
    provision_user_data_key(user=account, actor=account, master_key=master_key)
    return account


@pytest.mark.django_db
def test_claim_next_atomically_marks_oldest_available_job_running(user: Any) -> None:
    first = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="extract",
        available_at=timezone.now() - timedelta(seconds=2),
    )
    second = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="extract",
        available_at=timezone.now() - timedelta(seconds=1),
    )

    claimed = ProcessingJob.claim_next()

    assert claimed is not None
    assert claimed.pk == first.pk
    assert claimed.status == ProcessingJob.Status.RUNNING
    assert claimed.attempt_count == 1
    assert claimed.locked_at is not None
    assert ProcessingJob.claim_next().pk == second.pk  # type: ignore[union-attr]
    assert ProcessingJob.claim_next() is None


@pytest.mark.django_db
def test_retryable_failure_requeues_with_backoff_until_attempt_limit(user: Any) -> None:
    ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="extract",
        max_attempts=2,
    )
    claimed = ProcessingJob.claim_next()
    assert claimed is not None

    requeued = claimed.mark_failed(code="OCR_ENGINE_TIMEOUT", message="try again")

    assert requeued is True
    claimed.refresh_from_db()
    assert claimed.status == ProcessingJob.Status.QUEUED
    assert claimed.available_at > timezone.now()
    assert claimed.last_error_code == "OCR_ENGINE_TIMEOUT"
    assert claimed.last_error_message == "Text recognition took too long."

    claimed.available_at = timezone.now() - timedelta(seconds=1)
    claimed.save(update_fields=["available_at"])
    claimed = ProcessingJob.claim_next()
    assert claimed is not None
    assert claimed.attempt_count == 2
    assert claimed.mark_failed(code="OCR_ENGINE_TIMEOUT", message="final try") is False
    claimed.refresh_from_db()
    assert claimed.status == ProcessingJob.Status.FAILED


@pytest.mark.django_db
def test_stale_running_jobs_are_requeued(user: Any) -> None:
    job = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="extract",
        status=ProcessingJob.Status.RUNNING,
        locked_at=timezone.now() - timedelta(minutes=20),
    )

    recovered = ProcessingJob.recover_stale(cutoff=timezone.now() - timedelta(minutes=15))

    assert recovered == 1
    job.refresh_from_db()
    assert job.status == ProcessingJob.Status.QUEUED
    assert job.locked_at is None
    assert job.available_at <= timezone.now()


@pytest.mark.django_db
def test_worker_marks_unknown_tasks_failed(user: Any, capsys: Any) -> None:
    job = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="not_registered",
    )

    assert process_one_job() is True

    job.refresh_from_db()
    assert job.status == ProcessingJob.Status.FAILED
    assert job.last_error_code == "UNSUPPORTED_TASK"


@pytest.mark.django_db
def test_worker_dispatches_registered_handler_and_marks_success(
    user: Any, monkeypatch: Any
) -> None:
    handled: list[str] = []

    def handler(job: ProcessingJob) -> None:
        handled.append(str(job.id))

    monkeypatch.setitem(JOB_HANDLERS, "extract", handler)
    job = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="extract",
    )

    assert process_one_job() is True

    job.refresh_from_db()
    assert handled == [str(job.id)]
    assert job.status == ProcessingJob.Status.SUCCEEDED
    assert job.completed_at is not None


@pytest.mark.django_db
def test_worker_operation_uses_job_correlation_id(user: Any, monkeypatch: Any) -> None:
    observed_ids: list[str] = []

    def handler(job: ProcessingJob) -> None:
        observed_ids.append(request_id_context.get())

    monkeypatch.setitem(JOB_HANDLERS, "correlated", handler)
    job = ProcessingJob.objects.create(
        user=user,
        document_id=uuid4(),
        task_name="correlated",
    )

    assert process_one_job() is True

    assert observed_ids == [f"job-{job.id}"]
    assert request_id_context.get() == "-"


@pytest.mark.django_db
def test_process_document_jobs_once_exits_when_queue_is_empty(capsys: Any) -> None:
    call_command("process_document_jobs", once=True)
    assert capsys.readouterr().err == ""


@pytest.mark.django_db
def test_processing_jobs_are_scoped_to_the_authenticated_owner(user: Any) -> None:
    other = type(user).objects.create_user("other@example.com", password="password")
    own_job = ProcessingJob.objects.create(user=user, document_id=uuid4(), task_name="extract")
    ProcessingJob.objects.create(user=other, document_id=uuid4(), task_name="extract")

    assert list(ProcessingJob.for_user(user)) == [own_job]


@pytest.mark.django_db
def test_document_worker_updates_each_pipeline_phase(user: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "apps.processing.pipeline.execute_document_ocr",
        lambda **kwargs: (),
    )
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="worker.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.QUEUED,
    )
    directory = document_directory(document.pk)
    directory.mkdir(parents=True)
    path = directory / "original.png"
    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    path.write_bytes(image.getvalue())
    path.chmod(0o600)
    document.temporary_path = str(path)
    document.save(update_fields=["temporary_path"])
    job = ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
    )

    assert process_one_job() is True

    document.refresh_from_db()
    job.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.READY_FOR_REVIEW
    assert document.processing_attempt_count == 1
    assert job.status == ProcessingJob.Status.SUCCEEDED
    assert not path.exists()
    assert not directory.exists()


@pytest.mark.django_db
def test_stale_job_recovery_requeues_active_document(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="stale.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.OCR_RUNNING,
    )
    job = ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
        status=ProcessingJob.Status.RUNNING,
        locked_at=timezone.now() - timedelta(minutes=20),
    )

    recovered = ProcessingJob.recover_stale(cutoff=timezone.now() - timedelta(minutes=15))

    assert recovered == 1
    document.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.QUEUED
    job.refresh_from_db()
    assert job.status == ProcessingJob.Status.QUEUED


@pytest.mark.django_db
def test_worker_classifies_missing_temporary_file(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="missing.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.QUEUED,
    )
    job = ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
    )

    assert process_one_job() is True

    document.refresh_from_db()
    job.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.FAILED
    assert document.error_code == "TEMP_PATH_INVALID"
    assert job.last_error_code == "TEMP_PATH_INVALID"


@pytest.mark.django_db
def test_worker_requeues_failed_document_before_automatic_retry(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="retry.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.FAILED,
        error_code="TEMP_FILE_MISSING",
    )
    job = ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
    )

    process_one_job()

    document.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.FAILED
    assert document.processing_attempt_count == 1
    assert job.status == ProcessingJob.Status.QUEUED
