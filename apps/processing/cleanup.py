from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from .models import SourceDocument
from .storage import document_directory, safe_document_path

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {
    SourceDocument.Status.VALIDATING,
    SourceDocument.Status.PREPROCESSING,
    SourceDocument.Status.OCR_RUNNING,
    SourceDocument.Status.PARSING,
}


def cleanup_document_storage(document_id: UUID, temporary_path: str) -> bool:
    """Remove one document's temporary files; safe to call repeatedly."""

    if not temporary_path:
        return True
    try:
        path = safe_document_path(document_id, temporary_path)
        path.unlink(missing_ok=True)
        directory = document_directory(document_id)
        if directory.exists():
            directory.rmdir()
        SourceDocument.objects.filter(pk=document_id).update(cleanup_error_code="")
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Temporary cleanup failed for %s: %s", document_id, exc)
        SourceDocument.objects.filter(pk=document_id).update(cleanup_error_code="CLEANUP_FAILED")
        return False


def cleanup_stale_directories(*, cutoff: datetime) -> tuple[int, int]:
    """Remove old orphaned directories and return (removed, failed)."""

    root = Path(settings.DOCUMENT_TMP_ROOT)
    if not root.exists() or root.is_symlink():
        return 0, 0
    removed = 0
    failed = 0
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            document_id = UUID(directory.name)
        except ValueError:
            continue
        document = SourceDocument.objects.filter(pk=document_id).first()
        if document is not None and document.processing_status in ACTIVE_STATUSES:
            continue
        modified_at = datetime.fromtimestamp(
            directory.stat().st_mtime, tz=timezone.get_current_timezone()
        )
        if modified_at >= cutoff:
            continue
        try:
            shutil.rmtree(directory)
            removed += 1
        except OSError:
            failed += 1
            if document is not None:
                SourceDocument.objects.filter(pk=document.pk).update(
                    cleanup_error_code="CLEANUP_FAILED"
                )
    return removed, failed
