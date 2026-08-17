"""Open a backup archive and check it against its own manifest.

Cheap enough to run on every archive, which is the point: a backup nobody has
opened is a hypothesis, and the cost of testing it should not be a reason to
skip it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.core.backup import read_manifest, verify_backup

from .create_backup import PASSPHRASE_ENV


class Command(BaseCommand):
    help = "Check that a backup archive opens and matches its manifest."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("archive", help="Path to the archive to check.")

    def handle(self, *args: Any, **options: Any) -> None:
        passphrase = os.environ.get(PASSPHRASE_ENV, "")
        if not passphrase:
            raise CommandError(f"Set {PASSPHRASE_ENV} to the archive's passphrase.")
        path = Path(options["archive"])
        manifest = read_manifest(path, passphrase=passphrase)
        self.stdout.write(
            f"{path}: written {manifest.created_at}, "
            f"{sum(manifest.row_counts.values())} row(s), "
            f"{manifest.document_count} document(s)."
        )
        problems = verify_backup(path, passphrase=passphrase)
        for problem in problems:
            self.stderr.write(f"  {problem}")
        if problems:
            raise CommandError(f"{path}: {len(problems)} problem(s) found.")
        self.stdout.write(f"{path}: verified.")
