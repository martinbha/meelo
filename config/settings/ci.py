"""Settings for the CI run against PostgreSQL.

The suite runs on SQLite locally because it is fast, and on PostgreSQL in CI
because that is what production uses. The differences that matter are not
cosmetic: ``select_for_update`` is a no-op on SQLite, check constraints are
enforced differently, and ``JSONField`` lookups take different paths. A suite
that only ever runs on SQLite is a suite that has not tested the database the
data lives in (#269).
"""

from pathlib import Path

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "ci-only-secret-key"
ALLOWED_HOSTS = ["testserver"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "finance_ocr_ci",
        "USER": "finance_ci",
        "PASSWORD": "finance_ci",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        "CONN_MAX_AGE": 0,
    },
    # One database, two roles in production. In CI the second alias mirrors the
    # first so migrations run once and the shape still matches.
    "migration": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "finance_ocr_ci",
        "USER": "finance_ci",
        "PASSWORD": "finance_ci",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        "CONN_MAX_AGE": 0,
        "TEST": {"MIRROR": "default"},
    },
}

# The same path the SQLite settings use. Two tests assert against it directly,
# and the point of this module is to change the database, not the filesystem.
DOCUMENT_TMP_ROOT = Path("/tmp/finance-ocr-tests")
