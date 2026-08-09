from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.processing.retention import expire_documents


class Command(BaseCommand):
    help = "Delete source documents whose retention deadline has passed."

    def handle(self, *args: object, **options: object) -> None:
        expired = expire_documents()
        self.stdout.write(f"Deleted {expired} expired document(s).")
