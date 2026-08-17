from __future__ import annotations

from contextvars import ContextVar

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")

#: Set by the worker for each job it picks up. Carried on every log line and
#: metric so a failure in the worker can be joined to the request that queued the
#: work — which is the only way to answer "why did this document never finish"
#: without the two halves of the system having a shared identifier.
task_id_context: ContextVar[str] = ContextVar("task_id", default="-")
