import os
import signal
import socket
import uuid
from threading import Event

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.context import task_id_context
from apps.core.models import WorkerHeartbeat
from apps.processing.cleanup import cleanup_stale_directories
from apps.processing.services import process_one_job


class Command(BaseCommand):
    help = "Process queued document jobs from PostgreSQL."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process at most one job and exit when no work is available.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=1.0,
            help="Seconds to wait between empty queue polls.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        poll_interval = max(options["poll_interval"], 0.0)
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        shutdown = Event()

        removed, failed = cleanup_stale_directories(cutoff=timezone.now())
        if removed or failed:
            self.stdout.write(
                f"Startup cleanup removed {removed} orphaned director"
                f"{'y' if removed == 1 else 'ies'}; {failed} failed."
            )

        def request_shutdown(signum: int, frame: object) -> None:
            del signum, frame
            shutdown.set()

        previous = {
            signum: signal.signal(signum, request_shutdown)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            while not shutdown.is_set():
                # A signal received during processing records shutdown intent;
                # the synchronous handler finishes its transaction and cleanup
                # before this loop exits, so no document is abandoned halfway.
                token = task_id_context.set(uuid.uuid4().hex)
                try:
                    WorkerHeartbeat.touch(worker_id)
                    processed = process_one_job()
                    if processed:
                        WorkerHeartbeat.touch(worker_id, job_seen=True)
                finally:
                    task_id_context.reset(token)
                if options["once"] or not processed and poll_interval == 0:
                    return
                if not processed:
                    shutdown.wait(poll_interval)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
