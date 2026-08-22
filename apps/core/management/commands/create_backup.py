"""Write one sealed backup archive.

The passphrase is read from an environment variable rather than an argument, so
it does not land in shell history or in the process list where every other user
on the machine can read it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.backup import create_backup
from apps.core.key_management import KeyManagementError, load_master_key

PASSPHRASE_ENV = "MEELO_BACKUP_PASSPHRASE"


class Command(BaseCommand):
    help = "Write an encrypted backup of the database and retained screenshots."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("destination", help="Path to write the archive to.")
        parser.add_argument(
            "--skip-documents",
            action="store_true",
            help="Back up the database only, leaving retained screenshots out.",
        )
        parser.add_argument(
            "--retention-label",
            choices=("adhoc", "daily", "weekly", "monthly"),
            default="adhoc",
            help="Retention class recorded in the manifest.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        passphrase = os.environ.get(PASSPHRASE_ENV, "")
        if not passphrase:
            raise CommandError(
                f"Set {PASSPHRASE_ENV} to the passphrase this archive should be "
                f"sealed with. It is never stored, so keep it where you keep the "
                f"master key — separately from the archive itself."
            )
        document_root = (
            None
            if options["skip_documents"]
            else Path(getattr(settings, "DOCUMENT_TMP_ROOT", "") or ".")
        )
        try:
            master_key = load_master_key()
        except KeyManagementError:
            master_key = None
        manifest = create_backup(
            Path(options["destination"]),
            passphrase=passphrase,
            document_root=document_root,
            retention_label=options["retention_label"],
            master_key=master_key,
        )
        self.stdout.write(
            f"Wrote {options['destination']}: "
            f"{sum(manifest.row_counts.values())} row(s), "
            f"{manifest.document_count} document(s), "
            f"schema at {manifest.migration_count} migration(s)."
        )
        self.stdout.write(
            "The master key is not in this archive. Store it separately, or the "
            "archive is plaintext with extra steps."
        )
