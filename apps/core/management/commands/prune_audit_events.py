from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import AuditEvent


class Command(BaseCommand):
    help = "Delete audit events older than the configured retention period."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--days", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        days = options["days"]
        retention_days = int(
            str(days if days is not None else getattr(settings, "AUDIT_RETENTION_DAYS", 3650))
        )
        if retention_days < 1:
            self.stderr.write("Retention must be at least one day.")
            return
        cutoff = timezone.now() - timedelta(days=retention_days)
        queryset = AuditEvent.objects.filter(created_at__lt=cutoff)
        count = queryset.count()
        if not options["dry_run"]:
            queryset.delete()
        action = "Would delete" if options["dry_run"] else "Deleted"
        self.stdout.write(f"{action} {count} audit events.")
