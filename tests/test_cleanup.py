from __future__ import annotations

import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.processing.cleanup import (
    cleanup_document_storage,
    collect_cleanup_candidates,
    run_cleanup,
)
from apps.processing.models import SourceDocument
from apps.processing.storage import document_directory
from apps.reports.exports import safe_export_path
from apps.reports.models import TransactionExport


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("cleanup@example.com", password="password")


@pytest.fixture(autouse=True)
def clean_tmp() -> Any:
    yield
    shutil.rmtree("/tmp/finance-ocr-tests", ignore_errors=True)


@pytest.mark.django_db
def test_cleanup_is_idempotent_and_records_failures(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="cleanup.png",
        mime_type="image/png",
        file_size=4,
    )
    directory = document_directory(document.pk)
    directory.mkdir(parents=True)
    path = directory / "original.png"
    path.write_bytes(b"data")
    document.temporary_path = str(path)
    document.save(update_fields=["temporary_path"])

    assert cleanup_document_storage(document.pk, document.temporary_path) is True
    assert cleanup_document_storage(document.pk, document.temporary_path) is True
    assert not directory.exists()


@pytest.mark.django_db
def test_cleanup_command_removes_old_orphaned_directory(user: Any, capsys: Any) -> None:
    orphan = Path("/tmp/finance-ocr-tests") / str(uuid4())
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "stale.png").write_bytes(b"stale")
    old_timestamp = (timezone.now() - timedelta(hours=48)).timestamp()
    os.utime(orphan, (old_timestamp, old_timestamp))

    call_command("cleanup_document_files", age_hours=24)

    assert not orphan.exists()
    assert "Removed 1 stale document directory" in capsys.readouterr().out


@pytest.mark.django_db
def test_cleanup_does_not_remove_a_queued_document_directory(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="queued.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.QUEUED,
    )
    directory = document_directory(document.pk)
    directory.mkdir(parents=True)
    (directory / "original.png").write_bytes(b"queued")
    old_timestamp = (timezone.now() - timedelta(hours=48)).timestamp()
    os.utime(directory, (old_timestamp, old_timestamp))

    report = run_cleanup(cutoff=timezone.now() - timedelta(hours=24))

    assert report.removed == 0
    assert directory.exists()


@pytest.mark.django_db
def test_cleanup_dry_run_and_real_run_select_the_same_candidates(user: Any) -> None:
    orphan = Path("/tmp/finance-ocr-tests") / str(uuid4())
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "stale.png").write_bytes(b"stale")
    old_timestamp = (timezone.now() - timedelta(hours=48)).timestamp()
    os.utime(orphan, (old_timestamp, old_timestamp))

    export_path = safe_export_path("expired.csv")
    export_path.write_bytes(b"csv")
    export = TransactionExport.objects.create(
        user=user,
        export_format=TransactionExport.Format.CSV,
        file_path=str(export_path),
        expires_at=timezone.now() - timedelta(hours=1),
    )

    now = timezone.now()
    candidates = collect_cleanup_candidates(cutoff=now - timedelta(hours=24), now=now)
    dry_run = run_cleanup(cutoff=now - timedelta(hours=24), now=now, dry_run=True)
    real_run = run_cleanup(cutoff=now - timedelta(hours=24), now=now)

    assert dry_run.candidates == candidates
    assert real_run.candidates == candidates
    assert real_run.removed == 2
    assert real_run.failed == 0
    assert not orphan.exists()
    assert not export_path.exists()
    export.refresh_from_db()
    assert export.deleted_at is not None
