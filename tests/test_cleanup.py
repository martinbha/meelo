from __future__ import annotations

import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.processing.cleanup import (
    cleanup_document_storage,
    cleanup_stale_directories,
    collect_cleanup_candidates,
    run_cleanup,
)
from apps.processing.models import ProcessingJob, SourceDocument
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
def test_cleanup_does_not_remove_a_directory_claimed_by_a_running_job(user: Any) -> None:
    document = SourceDocument.objects.create(
        user=user,
        file_sha256=uuid4().hex + uuid4().hex,
        original_filename_encrypted="running.png",
        mime_type="image/png",
        file_size=4,
        processing_status=SourceDocument.Status.FAILED,
    )
    ProcessingJob.objects.create(
        user=user,
        document_id=document.pk,
        task_name="process_document",
        status=ProcessingJob.Status.RUNNING,
        locked_at=timezone.now(),
    )
    directory = document_directory(document.pk)
    directory.mkdir(parents=True)
    (directory / "decrypted.png").write_bytes(b"running")
    old_timestamp = (timezone.now() - timedelta(hours=48)).timestamp()
    os.utime(directory, (old_timestamp, old_timestamp))

    removed, failed = cleanup_stale_directories(cutoff=timezone.now())

    assert (removed, failed) == (0, 0)
    assert directory.exists()


@pytest.mark.django_db
def test_orphan_cleanup_failure_emits_identifier_metric(
    monkeypatch: Any,
) -> None:
    identifier = uuid4()
    orphan = Path("/tmp/finance-ocr-tests") / str(identifier)
    orphan.mkdir(parents=True, exist_ok=True)
    emitted: list[dict[str, object]] = []
    original_rmtree = shutil.rmtree

    def fail_removal(path: Path | str, ignore_errors: bool = False, **kwargs: object) -> None:
        del kwargs
        if Path(path) == orphan:
            raise OSError
        original_rmtree(path, ignore_errors=ignore_errors)

    def record(metric: str, **labels: object) -> None:
        emitted.append({"metric": metric, **labels})

    monkeypatch.setattr("apps.processing.cleanup.shutil.rmtree", fail_removal)
    monkeypatch.setattr("apps.processing.cleanup.metrics.record", record)

    removed, failed = cleanup_stale_directories(cutoff=timezone.now())

    assert (removed, failed) == (0, 1)
    assert any(
        payload["metric"] == "cleanup.failed" and payload["document_id"] == str(identifier)
        for payload in emitted
    )


@pytest.mark.django_db
def test_cleanup_command_exits_nonzero_when_removal_fails(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "apps.processing.management.commands.cleanup_document_files.cleanup_stale_directories",
        lambda *, cutoff: (0, 1),
    )

    with pytest.raises(CommandError, match=r"cleanup\(s\) failed"):
        call_command("cleanup_document_files")


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
