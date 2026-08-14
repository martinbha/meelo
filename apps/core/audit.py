from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from django.contrib.auth import get_user_model
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

    if event_type not in AuditEvent.EventType.values:
        raise ValueError(f"Unsupported audit event type: {event_type}")
    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        previous = (
            AuditEvent.objects.filter(user=locked_user).order_by("-created_at", "-id").first()
        )
        event = AuditEvent(
            user=locked_user,
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


def rebuild_audit_chain(user: Any) -> None:
    """Rebase a retained audit stream after authorized retention pruning."""

    with transaction.atomic():
        locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        previous_digest = ""
        for event in AuditEvent.objects.filter(user=locked_user).order_by("created_at", "id"):
            event.previous_digest = previous_digest
            event.digest = event.calculate_digest()
            event.save(update_fields=("previous_digest", "digest"))
            previous_digest = event.digest


def verify_audit_chain(user: Any) -> bool:
    """Verify every event digest and link for one user's audit stream."""

    previous_digest = ""
    for event in AuditEvent.objects.filter(user=user).order_by("created_at", "id"):
        if event.previous_digest != previous_digest or not event.verify_digest():
            return False
        previous_digest = event.digest
    return True
