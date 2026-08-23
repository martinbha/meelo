from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.reports.exports import ExportError
from apps.reports.models import TransactionExport
from apps.reports.services import export_file

from .models import ProcessingJob, SourceDocument
from .storage import document_directory, safe_document_path

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {
    SourceDocument.Status.PENDING,
    SourceDocument.Status.QUEUED,
    SourceDocument.Status.VALIDATING,
    SourceDocument.Status.PREPROCESSING,
    SourceDocument.Status.OCR_RUNNING,
    SourceDocument.Status.PARSING,
}


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    """One file-system object selected by the cleanup policy."""

    kind: str
    identifier: str
    path: Path | None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """The same candidate set is used for dry-run and deletion."""

    candidates: tuple[CleanupCandidate, ...]
    removed: int
    failed: int


def stale_document_candidates(*, cutoff: datetime) -> tuple[CleanupCandidate, ...]:
    """Find stale document directories without changing the file system."""

    root = Path(settings.DOCUMENT_TMP_ROOT)
    if not root.exists() or root.is_symlink():
        return ()
    candidates: list[CleanupCandidate] = []
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
        try:
            modified_at = datetime.fromtimestamp(
                directory.stat().st_mtime, tz=timezone.get_current_timezone()
            )
        except OSError:
            continue
        if modified_at < cutoff:
            candidates.append(CleanupCandidate("document_directory", str(document_id), directory))
    return tuple(candidates)


def expired_export_candidates(*, now: datetime | None = None) -> tuple[CleanupCandidate, ...]:
    """Find expired export files, including rows whose path is malformed."""

    moment = now or timezone.now()
    candidates: list[CleanupCandidate] = []
    for record in TransactionExport.objects.filter(
        deleted_at__isnull=True, expires_at__lte=moment
    ).order_by("expires_at", "pk"):
        try:
            path: Path | None = export_file(record)
        except ExportError:
            path = None
        candidates.append(CleanupCandidate("export_file", str(record.pk), path))
    return tuple(candidates)


def collect_cleanup_candidates(
    *, cutoff: datetime, now: datetime | None = None
) -> tuple[CleanupCandidate, ...]:
    """Return document and export candidates in the order an operator sees."""

    return (*stale_document_candidates(cutoff=cutoff), *expired_export_candidates(now=now))


def _remove_candidates(candidates: tuple[CleanupCandidate, ...]) -> tuple[int, int]:
    removed = 0
    failed = 0
    for candidate in candidates:
        try:
            if candidate.path is None:
                raise ValueError("The stored path is invalid.")
            if candidate.kind == "document_directory":
                with transaction.atomic():
                    running_job = (
                        ProcessingJob.objects.select_for_update()
                        .filter(
                            document_id=candidate.identifier,
                            status=ProcessingJob.Status.RUNNING,
                        )
                        .first()
                    )
                    document = (
                        SourceDocument.objects.select_for_update()
                        .filter(pk=candidate.identifier)
                        .first()
                    )
                    if running_job is not None or (
                        document is not None and document.processing_status in ACTIVE_STATUSES
                    ):
                        continue
                    if candidate.path.exists():
                        shutil.rmtree(candidate.path)
                    if document is not None:
                        document.cleanup_error_code = ""
                        document.save(update_fields=["cleanup_error_code"])
            else:
                record = TransactionExport.objects.get(pk=candidate.identifier)
                if candidate.path.exists():
                    candidate.path.unlink()
                record.deleted_at = timezone.now()
                record.save(update_fields=["deleted_at"])
            removed += 1
        except (ExportError, OSError, ValueError, TransactionExport.DoesNotExist) as exc:
            failed += 1
            logger.warning(
                "Cleanup failed for %s %s: %s", candidate.kind, candidate.identifier, exc
            )
            if candidate.kind == "document_directory":
                SourceDocument.objects.filter(pk=candidate.identifier).update(
                    cleanup_error_code="CLEANUP_FAILED"
                )
    return removed, failed


def run_cleanup(
    *, cutoff: datetime, now: datetime | None = None, dry_run: bool = False
) -> CleanupReport:
    """Report or remove one immutable candidate set."""

    candidates = collect_cleanup_candidates(cutoff=cutoff, now=now)
    if dry_run:
        return CleanupReport(candidates, removed=0, failed=0)
    removed, failed = _remove_candidates(candidates)
    return CleanupReport(candidates, removed=removed, failed=failed)


def cleanup_document_storage(document_id: UUID, temporary_path: str) -> bool:
    """Remove one document's temporary files; safe to call repeatedly."""

    if not temporary_path:
        return True
    try:
        safe_document_path(document_id, temporary_path)
        directory = document_directory(document_id)
        if directory.exists():
            if directory.is_symlink():
                raise ValueError("The document directory cannot be a symlink.")
            shutil.rmtree(directory)
        SourceDocument.objects.filter(pk=document_id).update(cleanup_error_code="")
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Temporary cleanup failed for %s: %s", document_id, exc)
        SourceDocument.objects.filter(pk=document_id).update(cleanup_error_code="CLEANUP_FAILED")
        return False


def cleanup_stale_directories(*, cutoff: datetime) -> tuple[int, int]:
    """Remove old orphaned directories and return (removed, failed)."""

    return _remove_candidates(stale_document_candidates(cutoff=cutoff))
