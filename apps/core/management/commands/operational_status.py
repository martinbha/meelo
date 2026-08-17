"""Report queue depth, cleanup health, and database latency.

A command rather than a dashboard, because the smallest useful version of
observability is a thing an operator can run over SSH at the moment something
looks wrong. Everything it prints is a count, a duration, or a status — never a
merchant, an amount, or a filename (specification 32).
"""

from __future__ import annotations

import json
import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count

from apps.core import metrics
from apps.observations.models import ImportedObservation
from apps.processing.models import SourceDocument


class Command(BaseCommand):
    help = "Report queue depth, cleanup failures, and database latency."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit one JSON object instead of a human-readable summary.",
        )
        parser.add_argument(
            "--emit-metrics",
            action="store_true",
            help="Also record the readings as metrics, for a scheduled run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        statuses = dict(
            SourceDocument.objects.values_list("processing_status").annotate(total=Count("pk"))
        )
        waiting = (
            SourceDocument.Status.PENDING,
            SourceDocument.Status.VALIDATING,
            SourceDocument.Status.QUEUED,
        )
        running = (
            SourceDocument.Status.PREPROCESSING,
            SourceDocument.Status.OCR_RUNNING,
            SourceDocument.Status.PARSING,
        )
        reading = {
            # Depth is what has not started, not what is in flight: a queue that
            # looks empty because everything is mid-OCR is not an empty queue.
            "queue_depth": sum(statuses.get(status, 0) for status in waiting),
            "processing": sum(statuses.get(status, 0) for status in running),
            "failed": statuses.get(SourceDocument.Status.FAILED, 0),
            "cleanup_failures": SourceDocument.objects.exclude(cleanup_error_code="").count(),
            "unreviewed_observations": ImportedObservation.objects.filter(
                review_status=ImportedObservation.ReviewStatus.UNREVIEWED
            ).count(),
            "database_latency_ms": latency_ms,
        }

        if options["emit_metrics"]:
            metrics.record(metrics.QUEUE_DEPTH, value=reading["queue_depth"], status="pending")
            metrics.record(metrics.CLEANUP_FAILED, value=reading["cleanup_failures"])
            metrics.record(metrics.DATABASE_LATENCY, value=latency_ms)

        if options["as_json"]:
            self.stdout.write(json.dumps(reading, sort_keys=True))
            return
        for name, value in sorted(reading.items()):
            self.stdout.write(f"{name}: {value}")
