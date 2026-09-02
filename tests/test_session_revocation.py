import hashlib
from typing import Any

import pytest
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.users.models import UserSession
from apps.users.security import security_overview
from tests.factories import make_user

pytestmark = pytest.mark.django_db
PASSWORD = "session-revocation-password"


@pytest.fixture
def owner() -> Any:
    return make_user(email="sessions@example.com", password=PASSWORD)


def tracked_client(owner: Any, *, ip: str, agent: str) -> Client:
    client = Client(REMOTE_ADDR=ip, HTTP_USER_AGENT=agent)
    client.force_login(owner)
    client.get(reverse("account-security"))
    return client


def test_session_metadata_is_hashed_and_lists_activity(owner: Any) -> None:
    client = tracked_client(owner, ip="192.0.2.10", agent="Private Browser/1")
    record = UserSession.objects.get(user=owner)

    assert record.ip_hash == hashlib.sha256(b"192.0.2.10").hexdigest()
    assert record.user_agent_hash == hashlib.sha256(b"Private Browser/1").hexdigest()
    assert "192.0.2.10" not in record.ip_hash
    overview = client.get(reverse("account-security")).context["overview"]
    assert overview.sessions[0].created_at is not None
    assert overview.sessions[0].last_activity_at is not None
    assert overview.sessions[0].is_current is True


def test_expired_session_is_not_listed_as_active(owner: Any) -> None:
    client = tracked_client(owner, ip="192.0.2.10", agent="expired")
    key = client.session.session_key
    assert key is not None
    Session.objects.filter(session_key=key).update(expire_date=timezone.now())

    assert not security_overview(owner, current_session_key=key).sessions


def test_revoke_one_rejects_that_session_on_its_next_request(owner: Any) -> None:
    current = tracked_client(owner, ip="192.0.2.1", agent="current")
    other = tracked_client(owner, ip="192.0.2.2", agent="other")
    other_key = other.session.session_key
    assert other_key is not None
    other_record = UserSession.objects.get(
        user=owner, session_key_hash=hashlib.sha256(other_key.encode()).hexdigest()
    )

    response = current.post(reverse("session-revoke", args=[other_record.pk]))

    assert response.status_code == 302
    assert other.get(reverse("transaction-list")).status_code == 302
    other_record.refresh_from_db()
    assert other_record.revoked_at is not None
    assert AuditEvent.objects.filter(user=owner, event_type="session_revoked").exists()


def test_revoke_all_others_preserves_current_session(owner: Any) -> None:
    current = tracked_client(owner, ip="192.0.2.1", agent="current")
    other = tracked_client(owner, ip="192.0.2.2", agent="other")

    assert current.post(reverse("sessions-revoke-others")).status_code == 302
    assert current.get(reverse("transaction-list")).status_code == 200
    assert other.get(reverse("transaction-list")).status_code == 302


def test_revoke_current_session_flushes_it(owner: Any) -> None:
    current = tracked_client(owner, ip="192.0.2.1", agent="current")
    record = UserSession.objects.get(user=owner)

    response = current.post(reverse("session-revoke", args=[record.pk]))

    assert response["Location"] == reverse("login")
    assert current.get(reverse("transaction-list")).status_code == 302


def test_password_change_revokes_every_session_including_current(owner: Any) -> None:
    current = tracked_client(owner, ip="192.0.2.1", agent="current")
    other = tracked_client(owner, ip="192.0.2.2", agent="other")

    response = current.post(
        reverse("password-change"),
        {
            "old_password": PASSWORD,
            "new_password1": "new session revocation password",
            "new_password2": "new session revocation password",
        },
    )

    assert response["Location"] == reverse("login")
    assert current.get(reverse("transaction-list")).status_code == 302
    assert other.get(reverse("transaction-list")).status_code == 302
