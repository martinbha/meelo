from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_proxy_sets_browser_security_headers_and_modern_tls() -> None:
    caddy = (PROJECT_ROOT / "Caddyfile").read_text()
    assert "protocols tls1.2 tls1.3" in caddy
    for header in (
        "Content-Security-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Strict-Transport-Security",
    ):
        assert header in caddy
    assert "script-src 'self'" in caddy
    assert "style-src 'self'" in caddy


def test_proxy_rejects_oversized_requests_before_django() -> None:
    caddy = (PROJECT_ROOT / "Caddyfile").read_text()
    assert "request_body" in caddy
    assert "max_size 20MB" in caddy
