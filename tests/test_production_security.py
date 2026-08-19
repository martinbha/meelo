import importlib
from typing import Any

import pytest


@pytest.fixture
def production_settings(monkeypatch: Any) -> Any:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("DJANGO_CSRF_TRUSTED_ORIGINS", "https://example.com")
    monkeypatch.setenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", "/run/secrets/test-master-key")
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
    monkeypatch.setenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", "/run/secrets/test-master-key")
    module = importlib.import_module("config.settings.production")

    assert importlib.reload(module).SECURE_SSL_REDIRECT is False


def test_production_rejects_invalid_boolean(monkeypatch: Any) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("DJANGO_SECURE_SSL_REDIRECT", "sometimes")
    monkeypatch.setenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", "/run/secrets/test-master-key")
    module = importlib.import_module("config.settings.production")

    with pytest.raises(RuntimeError, match="must be a boolean"):
        importlib.reload(module)


def test_production_does_not_require_the_key_path_to_be_configured(monkeypatch: Any) -> None:
    """A Docker secret and a systemd credential are found without a path.

    The path used to be a required environment variable, which enforced the
    wrong thing: that a variable was set, not that a key could be read. A
    deployment mounting ``/run/secrets/field_encryption_master_key`` had to
    name a path it did not choose, and one that named a path to a file that did
    not exist started perfectly happily.
    """

    monkeypatch.setenv("DJANGO_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "example.com")
    monkeypatch.delenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", raising=False)
    module = importlib.import_module("config.settings.production")

    importlib.reload(module)

    assert module.FIELD_ENCRYPTION_MASTER_KEY_FILE == ""
    # The guarantee moved to startup, where it checks the key rather than the
    # variable. tests/test_master_key_sources.py holds that end of it.
    assert module.FIELD_ENCRYPTION_MASTER_KEY_REQUIRED is True
