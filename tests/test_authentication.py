import re
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

from apps.core.models import AuditEvent


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        "owner@example.com", password="correct horse battery staple"
    )


@pytest.mark.django_db
def test_private_login_uses_email_and_creates_session(user: Any, client: Client) -> None:
    response = client.post(
        reverse("login"),
        {
            "username": "owner@example.com",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 302
    assert response["Location"] == "/"
    assert int(client.session["_auth_user_id"]) == user.pk
    assert user.audit_events.filter(event_type=AuditEvent.EventType.LOGIN_SUCCESS).exists()


@pytest.mark.django_db
def test_logout_clears_session(user: Any, client: Client) -> None:
    client.force_login(user)

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response["Location"] == "/login/"
    assert "_auth_user_id" not in client.session
    assert user.audit_events.filter(event_type=AuditEvent.EventType.LOGOUT).exists()


@pytest.mark.django_db
def test_login_page_renders_email_form(client: Client) -> None:
    response = client.get(reverse("login"))

    assert response.status_code == 200
    assert 'name="username"' in response.content.decode()
    assert 'type="email"' in response.content.decode()


@pytest.mark.django_db
@override_settings(AXES_FAILURE_LIMIT=3, AXES_COOLOFF_TIME=1)
def test_repeated_failed_logins_are_throttled(user: Any, client: Client) -> None:
    payload = {"username": user.email, "password": "incorrect"}

    first = client.post(reverse("login"), payload)
    second = client.post(reverse("login"), payload)
    third = client.post(reverse("login"), payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert "too many" in third.content.decode().lower()
    assert user.audit_events.filter(event_type=AuditEvent.EventType.LOGIN_FAILURE).exists()


@pytest.mark.django_db
def test_new_passwords_use_argon2id(user: Any) -> None:
    assert user.password.startswith("argon2$")


@pytest.mark.django_db
def test_authenticated_password_change_revokes_other_sessions(user: Any) -> None:
    initiating_client = Client()
    other_client = Client()
    initiating_client.force_login(user)
    other_client.force_login(user)

    response = initiating_client.post(
        reverse("password-change"),
        {
            "old_password": "correct horse battery staple",
            "new_password1": "new correct horse battery staple",
            "new_password2": "new correct horse battery staple",
        },
    )

    assert response.status_code == 302
    assert initiating_client.get(reverse("transaction-list")).status_code == 200
    assert other_client.get(reverse("transaction-list")).status_code == 302
    assert user.audit_events.filter(event_type=AuditEvent.EventType.PASSWORD_CHANGED).exists()


@pytest.mark.django_db
def test_password_reset_changes_password_and_revokes_existing_session(user: Any) -> None:
    existing_client = Client()
    existing_client.force_login(user)

    response = Client().post(reverse("password-reset"), {"email": user.email})

    assert response.status_code == 302
    assert len(mail.outbox) == 1
    reset_url = re.search(r"https?://\S+", str(mail.outbox[0].body))
    assert reset_url is not None
    reset_path = urlsplit(reset_url.group(0)).path

    reset_client = Client()
    token_response = reset_client.get(reset_path)
    assert token_response.status_code == 302
    set_password_path = token_response["Location"]
    completed = reset_client.post(
        set_password_path,
        {
            "new_password1": "reset correct horse battery staple",
            "new_password2": "reset correct horse battery staple",
        },
    )

    assert completed.status_code == 302
    user.refresh_from_db()
    assert user.check_password("reset correct horse battery staple")
    assert existing_client.get(reverse("transaction-list")).status_code == 302
    assert user.audit_events.filter(event_type=AuditEvent.EventType.PASSWORD_CHANGED).exists()
