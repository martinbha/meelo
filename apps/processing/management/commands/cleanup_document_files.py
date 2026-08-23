from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.processing.cleanup import cleanup_stale_directories, stale_document_candidates


class Command(BaseCommand):
    help = "Remove stale private document temporary directories."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--age-hours", type=int, default=24)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        age_hours = max(int(str(options["age_hours"])), 1)
        cutoff = timezone.now() - timedelta(hours=age_hours)
        if options["dry_run"]:
            candidates = stale_document_candidates(cutoff=cutoff)
            self.stdout.write(
                f"Would remove {len(candidates)} stale document director"
                f"{'y' if len(candidates) == 1 else 'ies'} older than {cutoff.isoformat()}."
            )
            return
        removed, failed = cleanup_stale_directories(cutoff=cutoff)
        self.stdout.write(
            f"Removed {removed} stale document director{'y' if removed == 1 else 'ies'}."
        )
        if failed:
            self.stderr.write(
                f"Failed to remove {failed} stale document director{'y' if failed == 1 else 'ies'}."
            )
            raise CommandError(f"{failed} stale document cleanup(s) failed.")
