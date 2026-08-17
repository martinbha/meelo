"""Unpack a backup archive, then load it if asked.

Unpacking and loading are separate steps on purpose. A restore rehearsal should
be able to look at what it got before deciding to replace anything, and the first
step of a real restore should be identical to the first step of a rehearsal —
otherwise the rehearsal is not testing the thing that will happen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core.backup import unpack_backup

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
        call_command("loaddata", str(report.database_path))
        self.stdout.write(
            "Loaded. The master key is not in the archive — restore it separately "
            "or the rows stay unreadable."
        )
