import os

from .base import *  # noqa: F403


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set in production")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
DEBUG = False
OCR_VERIFY_TESSERACT_INSTALLATION = True
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")  # noqa: F405
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must contain at least one host in production")

# The proxy terminates TLS and forwards the original protocol to Django.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")  # noqa: F405
# Optional, because a Docker secret at /run/secrets and a systemd credential are
# both found without configuration — a deployment using either should not have
# to name a path it did not choose. Naming one still wins: an operator who set
# it meant it, and silently reading a different file would be worse than
# failing. What is *not* optional is that a valid key is found at all, which is
# what the flag below enforces at startup (specification 22.1).
FIELD_ENCRYPTION_MASTER_KEY_FILE = os.getenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", "")
FIELD_ENCRYPTION_MASTER_KEY_REQUIRED = True
