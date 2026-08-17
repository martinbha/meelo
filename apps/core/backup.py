"""Backing the system up, and proving the backup is worth having.

A backup nobody has restored is a hypothesis. The commands here exist so the
hypothesis can be tested cheaply and often, on a disposable copy, without
touching the running system.

Three decisions shape the format.

**The master key is never in the backup.** It is the one thing that turns the
archive from ciphertext into somebody's finances. An archive containing both is
not an encrypted backup, it is a plaintext backup with extra steps — and it is
the file most likely to end up on a laptop, in cloud storage, or on a disk
somebody sells. The manifest says so explicitly, so a restore that cannot find a
key knows why rather than concluding the data is corrupt (specification 28, #258).

**The archive is sealed with its own passphrase.** Not the master key: a backup
has to remain readable after a key rotation, and it has to be restorable by
someone who has the archive and the passphrase without also handing them the
running system's secrets.

**The manifest is written first and read first.** It records what the archive
contains, which migration the schema was at, and how many rows of what — so a
restore can be checked against the system it came from before anything is
loaded, and a truncated archive is caught by the count rather than by a silent
half-restore.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.apps import apps
from django.core import serializers
from django.db.migrations.recorder import MigrationRecorder

from apps.reports.exports import ExportError, open_archive, seal_archive

#: Written into the manifest so a file found in three years explains itself.
BACKUP_FORMAT = "meelo-backup-v1"

#: Entry names inside the archive.
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.json"
DOCUMENTS_PREFIX = "documents/"

#: Apps whose rows are backed up. Sessions and admin logs are deliberately left
#: out: they are not history, and restoring a session would restore a login.
BACKED_UP_APPS: tuple[str, ...] = (
    "users",
    "core",
    "categorization",
    "financial_accounts",
    "instruments",
    "ledger",
    "observations",
    "ocr",
    "processing",
    "reconciliation",
    "reports",
    "transactions",
)


class BackupError(ExportError):
    """A backup cannot be produced or read."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """What an archive contains, and what it deliberately does not."""

    format: str
    created_at: str
    migration_count: int
    latest_migrations: dict[str, str]
    row_counts: dict[str, int]
    document_count: int
    #: Always false. Present so a restore reads a statement rather than an
    #: absence, and so a file claiming otherwise is visibly not ours.
    includes_master_key: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "created_at": self.created_at,
            "migration_count": self.migration_count,
            "latest_migrations": self.latest_migrations,
            "row_counts": self.row_counts,
            "document_count": self.document_count,
            "includes_master_key": self.includes_master_key,
            "master_key_note": (
                "The field-encryption master key is stored separately on purpose. "
                "Without it this archive is ciphertext; with it inside, the archive "
                "would be plaintext with extra steps."
            ),
        }


def _models() -> list[Any]:
    return [
        model
        for app_label in BACKED_UP_APPS
        for model in apps.get_app_config(app_label).get_models()
    ]


def build_manifest(*, document_count: int) -> BackupManifest:
    """Describe the system as it stands, before anything is written."""

    applied = MigrationRecorder.Migration.objects.order_by("app", "name")
    latest: dict[str, str] = {}
    for migration in applied:
        latest[migration.app] = migration.name
    return BackupManifest(
        format=BACKUP_FORMAT,
        created_at=datetime.now(UTC).isoformat(),
        migration_count=applied.count(),
        latest_migrations=latest,
        row_counts={
            f"{model._meta.app_label}.{model._meta.model_name}": model.objects.count()
            for model in _models()
        },
        document_count=document_count,
    )


def _document_paths(root: Path | None) -> list[Path]:
    if root is None or not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(payload))


def create_backup(
    destination: Path,
    *,
    passphrase: str,
    document_root: Path | None = None,
) -> BackupManifest:
    """Write one sealed archive, and return what went into it.

    The database rows are serialised rather than dumped with ``pg_dump`` so the
    archive is engine-independent: a restore into a disposable SQLite database
    for a rehearsal is the same operation as a restore into PostgreSQL, which is
    what makes rehearsing cheap enough to actually do.
    """

    documents = _document_paths(document_root)
    manifest = build_manifest(document_count=len(documents))

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        _add_bytes(
            archive,
            MANIFEST_NAME,
            json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2).encode(),
        )
        rows = io.StringIO()
        # Streamed rather than returned as one string: the caller already holds
        # the compressed archive in memory, and a second full copy of every row
        # is the one that would decide how large a database can be backed up.
        serializers.serialize("json", _all_objects(), indent=2, stream=rows)
        _add_bytes(archive, DATABASE_NAME, rows.getvalue().encode())
        for path in documents:
            relative = path.relative_to(document_root) if document_root else path.name
            # Screenshots are already encrypted at rest; they travel as they are.
            archive.add(path, arcname=f"{DOCUMENTS_PREFIX}{relative}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(seal_archive(payload.getvalue(), passphrase=passphrase))
    destination.chmod(0o600)
    return manifest


def _all_objects() -> Iterable[Any]:
    for model in _models():
        yield from model.objects.all().iterator()


def _open(path: Path, *, passphrase: str) -> bytes:
    try:
        return open_archive(path.read_bytes(), passphrase=passphrase)
    except ExportError as error:
        raise BackupError(str(error)) from error


def manifest_from(body: bytes) -> BackupManifest:
    """Read the manifest out of an already-opened archive body.

    Split from :func:`read_manifest` so a caller that has decrypted the archive
    does not pay for it twice. Opening one costs an Argon2id derivation —
    deliberately expensive, since it is the only thing between the archive and a
    reader — so doing it once per operation rather than once per question
    matters.
    """

    with tarfile.open(fileobj=io.BytesIO(body)) as archive:
        member = archive.extractfile(MANIFEST_NAME)
        if member is None:
            raise BackupError("The archive has no manifest.")
        payload = json.loads(member.read())
    if payload.get("format") != BACKUP_FORMAT:
        raise BackupError(f"Unknown backup format: {payload.get('format')!r}.")
    if payload.get("includes_master_key"):
        raise BackupError("This archive claims to contain the master key, which ours never do.")
    return BackupManifest(
        format=payload["format"],
        created_at=payload["created_at"],
        migration_count=payload["migration_count"],
        latest_migrations=payload["latest_migrations"],
        row_counts=payload["row_counts"],
        document_count=payload["document_count"],
    )


def read_manifest(path: Path, *, passphrase: str) -> BackupManifest:
    """Read what an archive claims to contain, without unpacking the rest."""

    return manifest_from(_open(path, passphrase=passphrase))


@dataclass
class RestoreReport:
    """What a restore unpacked, and anything it could not account for."""

    manifest: BackupManifest
    database_path: Path
    document_paths: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.problems


def unpack_backup(path: Path, *, passphrase: str, destination: Path) -> RestoreReport:
    """Unpack an archive into a directory, checking it against its own manifest.

    Deliberately does not load anything into a database. Unpacking and loading
    are separate so a rehearsal can inspect what it got before deciding to
    replace anything, and so a restore into a disposable environment is the same
    first step as a real one.
    """

    # One decryption for both the manifest and the contents.
    body = _open(path, passphrase=passphrase)
    manifest = manifest_from(body)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(body)) as archive:
        for member in archive.getmembers():
            # Refuse a path that would escape the destination. An archive is
            # untrusted input even when it is one we wrote.
            target = (destination / member.name).resolve()
            if not str(target).startswith(str(destination.resolve())):
                raise BackupError(f"Archive entry escapes the destination: {member.name}")
        archive.extractall(destination, filter="data")

    report = RestoreReport(
        manifest=manifest,
        database_path=destination / DATABASE_NAME,
        document_paths=sorted((destination / "documents").rglob("*"))
        if (destination / "documents").exists()
        else [],
    )
    if not report.database_path.exists():
        report.problems.append("The archive contains no database export.")
    files = [path for path in report.document_paths if path.is_file()]
    if len(files) != manifest.document_count:
        report.problems.append(
            f"Expected {manifest.document_count} document(s), found {len(files)}."
        )
    return report


def verify_backup(path: Path, *, passphrase: str) -> list[str]:
    """Check an archive opens and is internally consistent. Returns problems."""

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        report = unpack_backup(path, passphrase=passphrase, destination=Path(scratch))
        problems = list(report.problems)
        try:
            rows = json.loads(report.database_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"The database export is unreadable: {error}")
            return problems
        counted = len(rows)
        expected = sum(report.manifest.row_counts.values())
        if counted != expected:
            problems.append(f"Expected {expected} row(s) in the export, found {counted}.")
    return problems
