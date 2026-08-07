from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.processing.models import ProcessingJob


class Command(BaseCommand):
    help = "Requeue processing jobs abandoned by a stopped worker."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--timeout-minutes",
            type=int,
            default=15,
            help="Consider running jobs stale after this many minutes.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        timeout_minutes = max(options["timeout_minutes"], 1)
        cutoff = timezone.now() - timedelta(minutes=timeout_minutes)
        recovered = ProcessingJob.recover_stale(cutoff=cutoff)
        self.stdout.write(self.style.SUCCESS(f"Requeued {recovered} stale processing job(s)."))
