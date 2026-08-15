from __future__ import annotations

import logging
from collections.abc import Callable

from apps.core.context import request_id_context

from .models import ProcessingJob

logger = logging.getLogger(__name__)


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

    context_token = request_id_context.set(f"job-{job.id}")
    try:
        try:
            dispatch_job(job)
        except UnsupportedTaskError as exc:
            job.mark_failed(code="UNSUPPORTED_TASK", message=str(exc))
        except RetryableJobError as exc:
            job.mark_failed(code=exc.code, message=str(exc), retryable=True)
        except NonRetryableJobError as exc:
            job.mark_failed(code=exc.code, message=str(exc), retryable=False)
        except Exception as exc:  # pragma: no cover - exercised by integration handlers
            logger.exception("Processing job %s failed unexpectedly", job.id)
            job.mark_failed(code="UNHANDLED_ERROR", message=str(exc), retryable=True)
        else:
            job.mark_succeeded()
    finally:
        request_id_context.reset(context_token)
    return True
