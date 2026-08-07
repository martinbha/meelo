from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import reverse


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


@pytest.mark.django_db
def test_logout_clears_session(user: Any, client: Client) -> None:
    client.force_login(user)

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response["Location"] == "/login/"
    assert "_auth_user_id" not in client.session


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
