from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.processing.models import ProcessingJob
from apps.processing.services import JOB_HANDLERS, process_one_job


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


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

    requeued = claimed.mark_failed(code="TEMPORARY", message="try again", retryable=True)

    assert requeued is True
    claimed.refresh_from_db()
    assert claimed.status == ProcessingJob.Status.QUEUED
    assert claimed.available_at > timezone.now()
    assert claimed.last_error_code == "TEMPORARY"

    claimed.available_at = timezone.now() - timedelta(seconds=1)
    claimed.save(update_fields=["available_at"])
    claimed = ProcessingJob.claim_next()
    assert claimed is not None
    assert claimed.attempt_count == 2
    assert claimed.mark_failed(code="TEMPORARY", message="final try", retryable=True) is False
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
def test_process_document_jobs_once_exits_when_queue_is_empty(capsys: Any) -> None:
    call_command("process_document_jobs", once=True)
    assert capsys.readouterr().err == ""
