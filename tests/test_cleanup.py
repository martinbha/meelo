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

from apps.processing.cleanup import cleanup_document_storage
from apps.processing.models import SourceDocument
from apps.processing.storage import document_directory


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
