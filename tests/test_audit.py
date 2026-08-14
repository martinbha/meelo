from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.core.audit import record_audit_event, verify_audit_chain
from apps.core.models import AuditEvent


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("audit@example.com", password="password")


@pytest.mark.django_db
def test_audit_event_types_and_chain_verification(user: Any) -> None:
    first = record_audit_event(
        user=user,
        event_type=AuditEvent.EventType.SCREENSHOT_UPLOADED,
        metadata={"document_id": "doc-1", "mime_type": "image/png"},
        ip_address="192.0.2.1",
        user_agent="test-agent",
    )
    second = record_audit_event(
        user=user,
        event_type=AuditEvent.EventType.TRANSACTION_CORRECTED,
        metadata={"transaction_id": "txn-1"},
    )

    assert first.ip_hash != "192.0.2.1"
    assert first.user_agent_hash != "test-agent"
    assert second.previous_digest == first.digest
    assert verify_audit_chain(user) is True

    second.metadata = {"transaction_id": "changed"}
    second.save(update_fields=["metadata"])
    assert verify_audit_chain(user) is False


@pytest.mark.django_db
def test_audit_retention_command_deletes_only_expired_events(user: Any, capsys: Any) -> None:
    old = record_audit_event(user=user, event_type=AuditEvent.EventType.LOGOUT)
    recent = record_audit_event(user=user, event_type=AuditEvent.EventType.LOGIN_SUCCESS)
    old_time = timezone.now() - timedelta(days=10)
    AuditEvent.objects.filter(pk=old.pk).update(created_at=old_time)
    retained_digest = recent.digest

    call_command("prune_audit_events", days=7)

    assert not AuditEvent.objects.filter(pk=old.pk).exists()
    assert AuditEvent.objects.filter(pk=recent.pk).exists()
    assert "Deleted 1 audit events" in capsys.readouterr().out
    recent.refresh_from_db()
    assert recent.digest == retained_digest
    assert verify_audit_chain(user) is True
