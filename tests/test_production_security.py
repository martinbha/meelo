import importlib
from typing import Any

import pytest


@pytest.fixture
def production_settings(monkeypatch: Any) -> Any:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("DJANGO_CSRF_TRUSTED_ORIGINS", "https://example.com")
    module = importlib.import_module("config.settings.production")
    return importlib.reload(module)


def test_production_settings_enable_transport_and_browser_security(
    production_settings: Any,
) -> None:
    settings = production_settings

    assert settings.SECURE_SSL_REDIRECT is True
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SECURE_HSTS_SECONDS == 31536000
    assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert settings.SECURE_HSTS_PRELOAD is True
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SECURE_REFERRER_POLICY == "strict-origin-when-cross-origin"
    assert settings.X_FRAME_OPTIONS == "DENY"
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_TRUSTED_ORIGINS == ["https://example.com"]


def test_production_redirect_can_be_disabled_explicitly(monkeypatch: Any) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("DJANGO_SECURE_SSL_REDIRECT", "false")
    module = importlib.import_module("config.settings.production")

    assert importlib.reload(module).SECURE_SSL_REDIRECT is False


def test_production_rejects_invalid_boolean(monkeypatch: Any) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("DJANGO_SECURE_SSL_REDIRECT", "sometimes")
    module = importlib.import_module("config.settings.production")

    with pytest.raises(RuntimeError, match="must be a boolean"):
        importlib.reload(module)
