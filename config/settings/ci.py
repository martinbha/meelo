"""Settings for the CI run against PostgreSQL.

The suite runs on SQLite locally because it is fast, and on PostgreSQL in CI
because that is what production uses. The differences that matter are not
cosmetic: ``select_for_update`` is a no-op on SQLite, check constraints are
enforced differently, and ``JSONField`` lookups take different paths. A suite
that only ever runs on SQLite is a suite that has not tested the database the
data lives in (#269).
"""

# Inherits from ``testing`` rather than from ``base``, so this module differs
# from the local suite in the database and in nothing else. Inheriting from
# ``base`` meant every setting the test suite relaxes had to be relaxed twice,
# and the two drifted the first time one of them gained an entry — CI then
# failed on a difference that had nothing to do with PostgreSQL.
from .testing import *  # noqa: F403

SECRET_KEY = "ci-only-secret-key"

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
