import base64
import os
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.core.key_management import provision_user_data_key
from apps.ocr.pipeline import OcrPipelineError
from apps.processing.models import ProcessingJob, SourceDocument
from apps.processing.services import process_one_job
from apps.processing.storage import document_directory
from tests.factories import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def worker_user(tmp_path: Path, settings: Any) -> Any:
    key = os.urandom(32)
    key_path = tmp_path / "master.key"
    key_path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    key_path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(key_path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    user = make_user(email="failure-modes@example.com")
    provision_user_data_key(user=user, actor=user, master_key=key)
    return user


def _queued_document(user: Any, *, filename: str) -> tuple[SourceDocument, ProcessingJob, Path]:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=os.urandom(32).hex(),
        original_filename_encrypted="fixture.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.QUEUED,
        temporary_path="",
    )
    path = document_directory(document.pk) / filename
    document.temporary_path = str(path)
    document.save(update_fields=["temporary_path"])
    job = ProcessingJob.objects.create(
        user=user, document_id=document.pk, task_name="process_document"
    )
    return document, job, path


def test_missing_file_is_terminally_recorded_and_cleaned(worker_user: Any) -> None:
    document, job, path = _queued_document(worker_user, filename="original.png")

    assert process_one_job() is True

    document.refresh_from_db()
    job.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.FAILED
    assert document.error_code == "TEMP_FILE_MISSING"
    assert job.status == ProcessingJob.Status.QUEUED
    assert not path.exists()


def test_ocr_failure_leaves_no_partial_run_or_file(worker_user: Any, monkeypatch: Any) -> None:
    document, job, _ = _queued_document(worker_user, filename="original.png")
    path = Path(document.temporary_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    path.write_bytes(image.getvalue())

    def fail(**kwargs: Any) -> None:
        raise OcrPipelineError("engine timed out", code="OCR_ENGINE_TIMEOUT", retryable=True)

    monkeypatch.setattr("apps.processing.pipeline.execute_document_ocr", fail)
    assert process_one_job() is True

    document.refresh_from_db()
    job.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.FAILED
    assert document.error_code == "OCR_ENGINE_TIMEOUT"
    assert job.status == ProcessingJob.Status.QUEUED
    assert not path.exists()
    assert not path.parent.exists()
