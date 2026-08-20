from pathlib import Path

from .base import *  # noqa: F403

# Tests render templates from the source tree without running collectstatic.
# Production and the image use the manifest backend configured in base settings.
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEBUG = False
SECRET_KEY = "test-only-secret-key"
ALLOWED_HOSTS = ["testserver"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
    # Kept here as well as in production so the two aliases are exercised. The
    # mirror stops Django building a second test database for what is one
    # database reached by two roles.
    "migration": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"MIRROR": "default"},
    },
}
DOCUMENT_TMP_ROOT = Path("/tmp/finance-ocr-tests")

#: Off, so a fixture can build a row without a key store behind it. The
#: production setting is exercised deliberately by
#: ``tests/test_plaintext_encryption.py``, which turns it back on and drives the
#: real services through it — an off switch nothing tests is an off switch that
#: turns out to have been on.
FIELD_ENCRYPTION_REQUIRED = False
