import tomllib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.core.models import WorkerHeartbeat
from apps.processing.models import ProcessingJob, SourceDocument
from tests.factories import make_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_declares_supported_python() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert metadata["project"]["requires-python"] == ">=3.12"
    assert (PROJECT_ROOT / "uv.lock").is_file()


@pytest.mark.django_db
def test_health_check_reports_database_readiness(client: Client) -> None:
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "queue_depth": 0,
        "oldest_queued_age_seconds": -1.0,
        "stuck_documents": 0,
        "worker_heartbeat_age_seconds": -1.0,
        "worker_available": 0,
    }


@pytest.mark.django_db
def test_health_check_reports_stale_worker_queue_age_and_stuck_count(
    client: Client, settings: object
) -> None:
    settings.WORKER_HEARTBEAT_STALE_SECONDS = 60  # type: ignore[attr-defined]
    settings.PROCESSING_STUCK_AFTER_SECONDS = 300  # type: ignore[attr-defined]
    user = make_user(email="health-owner@example.com")
    old = timezone.now() - timedelta(minutes=10)
    job = ProcessingJob.objects.create(user=user, document_id=uuid4(), task_name="health-test")
    ProcessingJob.objects.filter(pk=job.pk).update(created_at=old)
    SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="hidden",
        mime_type="image/png",
        file_size=1,
        processing_status=SourceDocument.Status.OCR_RUNNING,
        processing_started_at=old,
    )
    WorkerHeartbeat.objects.create(worker_id="hidden-worker", last_seen_at=old)

    reading = client.get("/health/").json()

    assert reading["queue_depth"] == 1
    assert reading["oldest_queued_age_seconds"] >= 600
    assert reading["stuck_documents"] == 1
    assert reading["worker_heartbeat_age_seconds"] >= 600
    assert reading["worker_available"] == 0
    assert "hidden" not in str(reading)
