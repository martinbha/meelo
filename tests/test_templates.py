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


def test_base_shell_exposes_progress_indicator(client: Client) -> None:
    response = client.get(reverse("login"))

    assert 'id="global-progress"' in response.content.decode()
