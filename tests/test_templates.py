from typing import Any

import pytest
from django.test import Client
from django.urls import reverse


def test_login_uses_shared_template_shell(client: Client) -> None:
    response = client.get(reverse("login"))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'id="main-content"' in content
    assert 'hx-boost="true"' in content
    assert "htmx.org@2.0.4" in content
    assert 'href="/static/css/app.css"' in content
    assert 'src="/static/js/app.js"' in content
    assert 'name="csrfmiddlewaretoken"' in content
    assert '<form method="post" hx-boost="false">' in content


def test_base_shell_exposes_progress_indicator(client: Client) -> None:
    response = client.get(reverse("login"))

    assert 'id="global-progress"' in response.content.decode()


@pytest.mark.django_db
def test_authenticated_dashboard_uses_shared_shell(client: Client, django_user_model: Any) -> None:
    user = django_user_model.objects.create_user("shell@example.com", password="password")
    client.force_login(user)

    response = client.get(reverse("dashboard"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "<!doctype html>" in content
    assert 'id="dashboard-heading"' in content
    assert reverse("transaction-list") in content
    assert reverse("upload-list") in content
    assert f'<form action="{reverse("logout")}" method="post" hx-boost="false">' in content


@pytest.mark.django_db
def test_htmx_navigation_returns_content_without_duplicate_shell(
    client: Client, django_user_model: Any
) -> None:
    user = django_user_model.objects.create_user("partial@example.com", password="password")
    client.force_login(user)

    response = client.get(reverse("transaction-list"), HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="transactions-heading"' in content
    assert "<!doctype html>" not in content
    assert "<header" not in content
