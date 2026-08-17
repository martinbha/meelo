"""Creating, serving, and destroying exports.

Three rules hold this together:

- **A recent sign-in is required.** An export is the one action that turns a
  user's whole financial history into a file, so an abandoned session must not be
  enough to trigger it.
- **A plaintext export expires.** It is deleted on a timer whether or not anybody
  downloaded it, because the reason it exists — so a person can save it elsewhere
  — is finished within minutes and the risk is not.
- **Nobody else can reach it.** Downloads resolve through the owner's own
  queryset, so another user's export is a 404 rather than a permission error:
  they have no business learning it exists.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.errors import ConflictError, ForbiddenError

from .exports import (
    ExportError,
    export_rows,
    safe_export_path,
    seal_archive,
    write_csv,
    write_json,
)
from .models import DEFAULT_EXPORT_LIFETIME, TransactionExport
from .spending import reportable_transactions

#: How long ago a sign-in still counts as recent for an export.
DEFAULT_RECENT_AUTH_MAX_AGE = timedelta(minutes=30)


def recent_auth_max_age() -> timedelta:
    configured = getattr(settings, "EXPORT_RECENT_AUTH_MAX_AGE_SECONDS", None)
    if configured is None:
        return DEFAULT_RECENT_AUTH_MAX_AGE
    return timedelta(seconds=int(configured))


def assert_recent_authentication(user: Any) -> None:
    """Refuse an export to a session nobody has signed into lately.

    Measured from ``last_login``, which is the strongest signal available until
    an explicit re-authentication prompt exists (#175). Blunt but honest: it
    means an export needs a sign-in within the window, not merely a cookie that
    has survived one.
    """

    last_login = getattr(user, "last_login", None)
    if last_login is None:
        raise ForbiddenError("Sign in again before exporting your data.")
    if timezone.now() - last_login > recent_auth_max_age():
        raise ForbiddenError("Your sign-in is too old to export data. Sign in again.")


def _render(
    user: Any,
    *,
    export_format: str,
    start: date | None,
    end: date | None,
    data_key: bytes | None,
    passphrase: str,
) -> tuple[bytes, int]:
    """Build the file's bytes. Touches no disk and no row."""

    transactions = reportable_transactions(user, start=start, end=end).select_related(
        "category", "financial_account", "payment_instrument"
    )
    rows = list(export_rows(transactions, data_key=data_key))
    buffer = io.StringIO()
    if export_format == TransactionExport.Format.CSV:
        write_csv(rows, buffer)
        return buffer.getvalue().encode(), len(rows)
    write_json(rows, buffer, period_start=start, period_end=end)
    payload = buffer.getvalue().encode()
    if export_format == TransactionExport.Format.ENCRYPTED:
        payload = seal_archive(payload, passphrase=passphrase)
    return payload, len(rows)


def create_export(
    *,
    user: Any,
    export_format: str,
    start: date | None = None,
    end: date | None = None,
    data_key: bytes | None = None,
    passphrase: str = "",
    lifetime: timedelta | None = None,
) -> TransactionExport:
    """Write one export file and record it.

    Deliberately **not** one transaction around the whole thing. The row has to
    be committed before the file is written, because a rollback after
    ``write_bytes`` would leave a file on disk that no row describes — and a file
    nothing knows about is one the cleanup will never remove. So: commit the row,
    write the file, then record what was written. A failure in the middle marks
    the row deleted and removes the partial file, which is a state the cleanup
    understands.
    """

    if export_format not in TransactionExport.Format.values:
        raise ExportError(f"Unknown export format: {export_format!r}.")
    assert_recent_authentication(user)
    if export_format == TransactionExport.Format.ENCRYPTED and not passphrase:
        raise ExportError("An encrypted export needs a passphrase.")
    window = lifetime or DEFAULT_EXPORT_LIFETIME
    if window <= timedelta(0):
        # An export that has already expired can never be downloaded, so it is a
        # file on disk with no purpose and no reader.
        raise ExportError("An export lifetime must be positive.")

    record = TransactionExport.objects.create(
        user=user,
        export_format=export_format,
        file_path=str(safe_export_path(f"{export_format}-placeholder")),
        period_start=start,
        period_end=end,
        expires_at=timezone.now() + window,
    )
    path = safe_export_path(f"{record.pk}.{export_format}")
    try:
        payload, row_count = _render(
            user,
            export_format=export_format,
            start=start,
            end=end,
            data_key=data_key,
            passphrase=passphrase,
        )
        path.write_bytes(payload)
        # 0600 before anything else can look: the export root is already 0700,
        # but a world-readable file inside it is one misconfigured parent away
        # from public.
        path.chmod(0o600)
    except Exception:
        # Never leave a half-written export downloadable.
        _unlink(record)
        raise

    with db_transaction.atomic():
        record.file_path = str(path)
        record.file_size = len(payload)
        record.row_count = row_count
        record.save(update_fields=["file_path", "file_size", "row_count"])
        record_audit_event(
            user=user,
            event_type="export_created",
            obj=record,
            metadata={
                "format": export_format,
                "row_count": row_count,
                "byte_size": len(payload),
                "encrypted": export_format == TransactionExport.Format.ENCRYPTED,
                # A period is a date range, not a value. Amounts and merchants
                # never reach the audit log.
                "period_start": start.isoformat() if start else "",
                "period_end": end.isoformat() if end else "",
            },
        )
    return record


def available_exports(user: Any) -> Any:
    """This user's exports that can still be downloaded."""

    return TransactionExport.objects.filter(
        user_id=user.pk, deleted_at__isnull=True, expires_at__gt=timezone.now()
    )


@db_transaction.atomic
def read_export(export_id: Any, *, user: Any) -> tuple[TransactionExport, bytes]:
    """Hand an export to its owner, once, while it is still alive.

    Resolved through the owner's queryset, so another user's export cannot be
    told apart from one that never existed.
    """

    record = (
        TransactionExport.objects.select_for_update().filter(pk=export_id, user_id=user.pk).first()
    )
    if record is None:
        raise ForbiddenError("Export not found.")
    if record.deleted_at is not None:
        raise ConflictError("This export has already been deleted.")
    if record.is_expired:
        raise ConflictError("This export has expired. Generate a new one.")

    path = export_file(record)
    if not path.exists():
        raise ConflictError("The export file is no longer on disk.")
    payload = path.read_bytes()
    record.downloaded_at = timezone.now()
    record.save(update_fields=["downloaded_at"])
    record_audit_event(
        user=user,
        event_type="export_downloaded",
        obj=record,
        metadata={"format": record.export_format, "byte_size": record.file_size},
    )
    return record, payload


@db_transaction.atomic
def delete_export(export_id: Any, *, user: Any) -> TransactionExport:
    """Remove one export's file now, without waiting for its expiry."""

    record = (
        TransactionExport.objects.select_for_update().filter(pk=export_id, user_id=user.pk).first()
    )
    if record is None:
        raise ForbiddenError("Export not found.")
    _unlink(record)
    return record


def purge_expired_exports(*, now: Any = None) -> int:
    """Delete every export file past its expiry. Returns how many went.

    Runs without a user: the whole point is that a file the user forgot about
    still disappears. The row survives so the audit trail keeps its shape.
    """

    moment = now or timezone.now()
    expired = TransactionExport.objects.filter(deleted_at__isnull=True, expires_at__lte=moment)
    removed = 0
    for record in expired:
        _unlink(record)
        removed += 1
    return removed


def export_file(record: TransactionExport) -> Path:
    """Where this export's file is, validated to be inside the export root.

    Read from the row rather than rebuilt from the identifier, so the stored path
    is the single answer to "where is it" — and still checked against the root,
    because a path in a database column is input like any other.
    """

    return safe_export_path(Path(record.file_path).name)


def _unlink(record: TransactionExport) -> None:
    """Remove the file and mark the row, tolerating a file already gone."""

    try:
        path: Path | None = export_file(record)
    except ExportError:
        path = None
    if path is not None and path.exists():
        path.unlink()
    record.deleted_at = timezone.now()
    record.save(update_fields=["deleted_at"])
