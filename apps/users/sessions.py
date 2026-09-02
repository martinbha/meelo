from __future__ import annotations

import hashlib
from typing import Any

from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit_event

from .models import User, UserSession


def session_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def track_session(*, user: User, session_key: str, ip_address: str, user_agent: str) -> UserSession:
    record, _ = UserSession.objects.update_or_create(
        session_key_hash=session_key_hash(session_key),
        defaults={
            "user": user,
            "ip_hash": session_key_hash(ip_address),
            "user_agent_hash": session_key_hash(user_agent),
            "last_activity_at": timezone.now(),
            "revoked_at": None,
        },
    )
    return record


def _delete_django_session(key_hash: str) -> None:
    for session in Session.objects.all().only("session_key"):
        if session_key_hash(session.session_key) == key_hash:
            session.delete()
            return


def active_session_hashes() -> set[str]:
    return {
        session_key_hash(key)
        for key in Session.objects.filter(expire_date__gt=timezone.now()).values_list(
            "session_key", flat=True
        )
    }


@transaction.atomic
def revoke_session(*, user: User, session_id: Any, current_session_key: str = "") -> bool:
    record = UserSession.objects.select_for_update().get(pk=session_id, user=user)
    is_current = bool(current_session_key) and record.session_key_hash == session_key_hash(
        current_session_key
    )
    _delete_django_session(record.session_key_hash)
    record.revoked_at = timezone.now()
    record.save(update_fields=("revoked_at",))
    record_audit_event(user=user, event_type="session_revoked", obj=record)
    return is_current


@transaction.atomic
def revoke_other_sessions(*, user: User, current_session_key: str) -> int:
    current_hash = session_key_hash(current_session_key)
    records = list(
        UserSession.objects.select_for_update()
        .filter(user=user, revoked_at__isnull=True)
        .exclude(session_key_hash=current_hash)
    )
    for record in records:
        _delete_django_session(record.session_key_hash)
    UserSession.objects.filter(pk__in=[record.pk for record in records]).update(
        revoked_at=timezone.now()
    )
    record_audit_event(
        user=user,
        event_type="session_revoked",
        metadata={"scope": "others", "count": len(records)},
    )
    return len(records)


@transaction.atomic
def revoke_all_sessions(*, user: User) -> int:
    records = list(
        UserSession.objects.select_for_update().filter(user=user, revoked_at__isnull=True)
    )
    for record in records:
        _delete_django_session(record.session_key_hash)
    UserSession.objects.filter(pk__in=[record.pk for record in records]).update(
        revoked_at=timezone.now()
    )
    return len(records)
