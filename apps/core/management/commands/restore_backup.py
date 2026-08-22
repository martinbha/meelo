"""Unpack a backup archive, then load it if asked.

Unpacking and loading are separate steps on purpose. A restore rehearsal should
be able to look at what it got before deciding to replace anything, and the first
step of a real restore should be identical to the first step of a rehearsal —
otherwise the rehearsal is not testing the thing that will happen.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.migrations.recorder import MigrationRecorder

from apps.core.backup import BACKED_UP_APPS, BackupManifest, unpack_backup
from apps.core.key_management import KeyManagementError, load_master_key
from apps.core.management.commands.master_key import verify_wrapped_keys

from .create_backup import PASSPHRASE_ENV


class Command(BaseCommand):
    help = "Unpack an encrypted backup, and optionally load it into this database."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("archive", help="Path to the archive to restore.")
        parser.add_argument("destination", help="Directory to unpack into.")
        parser.add_argument(
            "--load",
            action="store_true",
            help="Load the unpacked rows into this database. Run migrations first.",
        )
        parser.add_argument(
            "--allow-non-empty",
            action="store_true",
            help="Permit loading over existing application rows after an explicit review.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        passphrase = os.environ.get(PASSPHRASE_ENV, "")
        if not passphrase:
            raise CommandError(f"Set {PASSPHRASE_ENV} to the archive's passphrase.")
        report = unpack_backup(
            Path(options["archive"]),
            passphrase=passphrase,
            destination=Path(options["destination"]),
        )
        self.stdout.write(
            f"Unpacked to {options['destination']}: "
            f"{len(report.document_paths)} document path(s), "
            f"manifest written {report.manifest.created_at}."
        )
        for problem in report.problems:
            self.stderr.write(f"  {problem}")
        if not report.is_clean:
            raise CommandError("The archive does not match its manifest; nothing loaded.")

        if not options["load"]:
            self.stdout.write("Nothing was loaded. Re-run with --load to apply it.")
            return
        self._verify_migrations(report.manifest)
        if self._database_has_rows() and not options["allow_non_empty"]:
            raise CommandError(
                "The destination database is not empty. Inspect it and re-run with "
                "--allow-non-empty if replacing or merging these rows is intentional."
            )
        try:
            master_key = load_master_key()
        except KeyManagementError as error:
            raise CommandError(
                f"The master key must be restored separately before loading: {error}"
            ) from error
        with transaction.atomic():
            call_command("loaddata", str(report.database_path))
            verification = verify_wrapped_keys(master_key=master_key)
            if not verification.is_clean:
                raise CommandError(
                    f"{len(verification.unreadable)} restored wrapped key(s) could not be opened."
                )
        self._restore_documents(report)
        self.stdout.write(
            "Loaded. The master key is not in the archive — restore it separately "
            "or the rows stay unreadable."
        )

    def _database_has_rows(self) -> bool:
        return any(
            model._default_manager.exists()
            for app_label in BACKED_UP_APPS
            for model in apps.get_app_config(app_label).get_models()
        )

    def _verify_migrations(self, manifest: BackupManifest) -> None:
        current: dict[str, str] = {}
        for migration in MigrationRecorder.Migration.objects.order_by("app", "name"):
            current[migration.app] = migration.name
        missing = [
            app for app, name in manifest.latest_migrations.items() if current.get(app) != name
        ]
        if missing:
            raise CommandError(
                "The destination schema is behind the backup for: " + ", ".join(sorted(missing))
            )

    def _restore_documents(self, report: Any) -> None:
        root = Path(settings.DOCUMENT_TMP_ROOT)
        source_root = report.database_path.parent / "documents"
        if not source_root.exists():
            return
        root.mkdir(parents=True, exist_ok=True)
        for source in source_root.rglob("*"):
            relative = source.relative_to(source_root)
            target = root / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
