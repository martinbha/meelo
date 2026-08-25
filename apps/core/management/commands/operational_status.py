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

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection
from django.db.models import Count

from apps.core import metrics
from apps.core.operational_health import worker_queue_summary
from apps.observations.models import ImportedObservation
from apps.processing.models import SourceDocument


class Command(BaseCommand):
    help = "Report queue depth, worker health, cleanup failures, and database latency."

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
        for name, help_text in (
            ("queue-depth", "Maximum queued job count."),
            ("failed-documents", "Maximum failed document count."),
            ("cleanup-failures", "Maximum cleanup failure count."),
            ("unreviewed-observations", "Maximum unreviewed observation count."),
            ("database-latency-ms", "Maximum database probe latency."),
            ("worker-heartbeat-age-seconds", "Maximum age of the newest worker heartbeat."),
        ):
            parser.add_argument(
                f"--max-{name}",
                type=float,
                default=None,
                help=help_text,
            )

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.perf_counter()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            database_available = 1
        except DatabaseError as error:
            latency_ms = -1
            database_available = 0
            _record_database_failure(error)

        if not database_available:
            reading = {
                "queue_depth": -1,
                "oldest_queued_age_seconds": -1,
                "stuck_documents": -1,
                "processing": -1,
                "failed": -1,
                "cleanup_failures": -1,
                "unreviewed_observations": -1,
                "database_latency_ms": latency_ms,
                "database_available": database_available,
                "worker_heartbeat_age_seconds": -1,
                "worker_available": 0,
                "healthy": 0,
            }
            self._write(reading, options)
            raise CommandError("The database health probe failed.")

        statuses = dict(
            SourceDocument.objects.values_list("processing_status").annotate(total=Count("pk"))
        )
        running = (
            SourceDocument.Status.PREPROCESSING,
            SourceDocument.Status.OCR_RUNNING,
            SourceDocument.Status.PARSING,
        )
        reading = {
            # Depth is what has not started, not what is in flight: a queue that
            # looks empty because everything is mid-OCR is not an empty queue.
            "processing": sum(statuses.get(status, 0) for status in running),
            "failed": statuses.get(SourceDocument.Status.FAILED, 0),
            "cleanup_failures": SourceDocument.objects.exclude(cleanup_error_code="").count(),
            "unreviewed_observations": ImportedObservation.objects.filter(
                review_status=ImportedObservation.ReviewStatus.UNREVIEWED
            ).count(),
            "database_latency_ms": latency_ms,
            "database_available": database_available,
            **worker_queue_summary(),
        }

        reading["healthy"] = int(not self._violations(reading, options))

        if options["emit_metrics"]:
            metrics.record(metrics.QUEUE_DEPTH, value=reading["queue_depth"], status="pending")
            metrics.record(metrics.CLEANUP_FAILED, value=reading["cleanup_failures"])
            metrics.record(metrics.DATABASE_LATENCY, value=latency_ms)
            if not reading["worker_available"]:
                metrics.record(metrics.QUEUE_DEPTH, value=0, status="worker_missing")

        self._write(reading, options)
        if not reading["healthy"]:
            raise CommandError("One or more requested health thresholds were breached.")

    def _write(self, reading: dict[str, Any], options: dict[str, Any]) -> None:
        if options["as_json"]:
            self.stdout.write(json.dumps(reading, sort_keys=True))
            return
        for name, value in sorted(reading.items()):
            self.stdout.write(f"{name}: {value}")

    def _violations(self, reading: dict[str, Any], options: dict[str, Any]) -> list[str]:
        checks = {
            "queue_depth": options["max_queue_depth"],
            "failed": options["max_failed_documents"],
            "cleanup_failures": options["max_cleanup_failures"],
            "unreviewed_observations": options["max_unreviewed_observations"],
            "database_latency_ms": options["max_database_latency_ms"],
            "worker_heartbeat_age_seconds": options["max_worker_heartbeat_age_seconds"],
        }
        return [
            name
            for name, threshold in checks.items()
            if threshold is not None and (reading[name] < 0 or reading[name] > threshold)
        ]


def _record_database_failure(error: DatabaseError) -> None:
    kind = type(error).__name__.casefold()
    message = str(error).casefold()
    reason = "pool_exhausted" if "pool" in kind or "pool" in message else "connection"
    metric = (
        metrics.DATABASE_POOL_EXHAUSTED
        if reason == "pool_exhausted"
        else metrics.DATABASE_CONNECTION_FAILED
    )
    metrics.record(metric, reason=reason)
