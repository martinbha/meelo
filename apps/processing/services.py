from __future__ import annotations

import logging
import re
from collections.abc import Callable

from apps.core.context import (
    document_id_context,
    job_id_context,
    request_id_context,
    task_id_context,
)
from apps.core.key_scope import clear_scope

from .models import ProcessingJob

logger = logging.getLogger(__name__)
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class UnsupportedTaskError(Exception):
    """Raised when a queued task has no registered handler."""


class RetryableJobError(Exception):
    """Raised by a handler when a transient failure should be retried."""

    def __init__(self, message: str, *, code: str = "RETRYABLE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class NonRetryableJobError(Exception):
    """Raised by a handler when repeating the job cannot resolve the failure."""

    def __init__(self, message: str, *, code: str = "PERMANENT_ERROR") -> None:
        super().__init__(message)
        self.code = code


JobHandler = Callable[[ProcessingJob], None]
JOB_HANDLERS: dict[str, JobHandler] = {}


def register_handler(task_name: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(handler: JobHandler) -> JobHandler:
        JOB_HANDLERS[task_name] = handler
        return handler

    return decorator


def dispatch_job(job: ProcessingJob) -> None:
    if not job.user_id:
        raise UnsupportedTaskError("Processing jobs must have an owner.")
    try:
        handler = JOB_HANDLERS[job.task_name]
    except KeyError as exc:
        raise UnsupportedTaskError(f"No handler registered for task '{job.task_name}'") from exc
    handler(job)


def process_one_job() -> bool:
    """Claim and process one available job; return whether work was found."""

    job = ProcessingJob.claim_next()
    if job is None:
        return False

    payload = job.payload if isinstance(job.payload, dict) else {}
    queued_request_id = str(payload.get("request_id", ""))
    if queued_request_id == "-" or not _CORRELATION_ID.fullmatch(queued_request_id):
        queued_request_id = f"job-{job.id}"
    request_token = request_id_context.set(queued_request_id)
    task_token = task_id_context.set(str(job.id))
    job_token = job_id_context.set(str(job.id))
    document_token = document_id_context.set(str(job.document_id))
    try:
        try:
            dispatch_job(job)
        except UnsupportedTaskError as exc:
            job.mark_failed(code="UNSUPPORTED_TASK", message=str(exc))
        except RetryableJobError as exc:
            job.mark_failed(code=exc.code, message=str(exc))
        except NonRetryableJobError as exc:
            job.mark_failed(code=exc.code, message=str(exc))
        except Exception as exc:  # pragma: no cover - exercised by integration handlers
            logger.exception("Processing job %s failed unexpectedly", job.id)
            job.mark_failed(code="UNHANDLED_ERROR", message=str(exc))
        else:
            job.mark_succeeded()
    finally:
        document_id_context.reset(document_token)
        job_id_context.reset(job_token)
        task_id_context.reset(task_token)
        request_id_context.reset(request_token)
        # Whatever the job did, it does not get to leave an unwrapped key in
        # the worker process for the next document — which may belong to
        # somebody else entirely.
        clear_scope()
    return True
