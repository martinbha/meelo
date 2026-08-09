from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from django.db import transaction

from .context import request_id_context
from .models import AuditEvent


def _hash_value(value: str | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()


def record_audit_event(
    *,
    user: Any,
    event_type: str,
    obj: Any | None = None,
    metadata: Mapping[str, object] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent:
    """Create a chained event containing identifiers and non-sensitive metadata only."""

    with transaction.atomic():
        previous = AuditEvent.objects.filter(user=user).order_by("-created_at", "-id").first()
        event = AuditEvent(
            user=user,
            event_type=event_type,
            object_type=f"{obj._meta.app_label}.{obj._meta.model_name}" if obj else "",
            object_id=getattr(obj, "pk", None),
            request_id=request_id_context.get(),
            ip_hash=_hash_value(ip_address),
            user_agent_hash=_hash_value(user_agent),
            metadata=dict(metadata or {}),
            previous_digest=previous.digest if previous else "",
        )
        event.save()
        return event
