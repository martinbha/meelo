"""Delete export files whose expiry has passed.

Scheduled rather than triggered by a user, because the risk this removes is
exactly the one a user has forgotten about: a plaintext CSV of their finances
sitting on disk long after they saved it somewhere else.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.reports.services import purge_expired_exports


class Command(BaseCommand):
    help = "Delete generated export files whose expiry has passed."

    def handle(self, *args: Any, **options: Any) -> None:
        removed = purge_expired_exports()
        self.stdout.write(f"Removed {removed} expired export file(s).")
