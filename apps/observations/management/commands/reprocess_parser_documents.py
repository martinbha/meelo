from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.core.errors import InvalidRequestError
from apps.observations.models import ImportedObservation
from apps.observations.reprocessing import request_reprocess
from apps.processing.models import SourceDocument


class Command(BaseCommand):
    help = "Requeue documents imported by a selected institution parser or parser version."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--institution")
        parser.add_argument("--parser-version")

    def handle(self, *args: Any, **options: Any) -> None:
        institution = options["institution"]
        parser_version = options["parser_version"]
        if not institution and not parser_version:
            raise CommandError("Provide --institution or --parser-version.")

        observations = ImportedObservation.objects.all()
        if institution:
            observations = observations.filter(parser_name=institution)
        if parser_version:
            observations = observations.filter(parser_version=parser_version)
        document_ids = observations.values_list("source_document_id", flat=True).distinct()
        documents = SourceDocument.objects.filter(pk__in=document_ids).select_related("user")

        queued = skipped = 0
        for document in documents.order_by("pk"):
            try:
                request_reprocess(document.pk, user=document.user)
            except InvalidRequestError as error:
                skipped += 1
                self.stderr.write(f"Skipped {document.pk}: {error}")
            else:
                queued += 1
        self.stdout.write(self.style.SUCCESS(f"Queued {queued} document(s); skipped {skipped}."))
