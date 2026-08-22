from __future__ import annotations

from typing import Any

from apps.core.sentry import configure_sentry, scrub_event


def test_sentry_scrubber_drops_sensitive_nested_fields() -> None:
    event: dict[str, Any] = {
        "message": "safe failure",
        "request": {
            "data": {"merchant": "hidden", "amount": "4200"},
            "cookies": {"sessionid": "hidden"},
            "url": "https://example.test/health/",
        },
        "contexts": {"ocr": {"text": "hidden"}, "trace": {"op": "health"}},
        "extra": {"safe": "kept"},
    }

    scrubbed = scrub_event(event)

    assert scrubbed == {
        "message": "safe failure",
        "request": {"url": "https://example.test/health/"},
        "contexts": {"trace": {"op": "health"}},
        "extra": {"safe": "kept"},
    }


def test_sentry_is_disabled_without_a_dsn(settings: Any) -> None:
    settings.SENTRY_DSN = ""

    assert configure_sentry() is False


def test_sentry_configuration_is_strict_when_enabled(settings: Any, monkeypatch: Any) -> None:
    import sentry_sdk

    settings.SENTRY_DSN = "https://public@example.test/1"
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **options: captured.update(options))

    assert configure_sentry() is True
    assert captured["request_bodies"] == "never"
    assert captured["send_default_pii"] is False
    assert captured["with_locals"] is False
    assert captured["before_send"] is scrub_event
