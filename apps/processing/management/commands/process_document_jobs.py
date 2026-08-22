import os
import socket
import time
import uuid

from django.core.management.base import BaseCommand

from apps.core.context import task_id_context
from apps.core.models import WorkerHeartbeat
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
        while True:
            # A fresh identifier per attempt, stamped on every log line and
            # metric the job produces. Without it, a failure here and the upload
            # that caused it are two unrelated entries in two different logs.
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
                time.sleep(poll_interval)
