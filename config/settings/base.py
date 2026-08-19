import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def env_list(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-secret-key")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    # Two-factor storage, installed ahead of the flow that uses it (#171-#173).
    # The device tables exist and the middleware annotates each request with
    # whether the user has verified, but nothing yet *requires* verification —
    # a login wall that arrives before enrolment does is a locked account.
    "django_otp",
    "django_otp.plugins.otp_totp",
    # Static devices hold the recovery codes #172 issues. Installed now so the
    # table is created by the same migration pass rather than a later one that
    # runs while somebody is locked out.
    "django_otp.plugins.otp_static",
    "apps.core.apps.CoreConfig",
    "apps.categorization.apps.CategorizationConfig",
    "apps.financial_accounts.apps.FinancialAccountsConfig",
    "apps.instruments.apps.InstrumentsConfig",
    "apps.ledger.apps.LedgerConfig",
    "apps.observations.apps.ObservationsConfig",
    "apps.ocr.apps.OcrConfig",
    "apps.parsing.apps.ParsingConfig",
    "apps.processing.apps.ProcessingConfig",
    "apps.reconciliation.apps.ReconciliationConfig",
    "apps.reports.apps.ReportsConfig",
    "apps.transactions.apps.TransactionsConfig",
    "apps.users.apps.UsersConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
    "apps.core.middleware.ErrorHandlingMiddleware",
    # Outside the view layer, so the key is cleared even when a view raises.
    "apps.core.middleware.DataKeyScopeMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Immediately after authentication, because it reads request.user and puts
    # request.user.is_verified() beside it. It verifies nothing on its own and
    # blocks nothing; enforcement is #173.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.htmx_template",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

_DATABASE_COMMON = {
    "ENGINE": os.getenv("DATABASE_ENGINE", "django.db.backends.postgresql"),
    "NAME": os.getenv("POSTGRES_DB", "finance_ocr"),
    "HOST": os.getenv("POSTGRES_HOST", "localhost"),
    "PORT": os.getenv("POSTGRES_PORT", "5432"),
}

DATABASES = {
    # What the web and worker processes use. This role can read and write rows
    # and nothing else: it cannot create a table, drop one, or grant itself more.
    "default": {
        **_DATABASE_COMMON,
        "USER": os.getenv("POSTGRES_USER", "finance_app"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "CONN_MAX_AGE": int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
    },
    # Schema changes only, and only while a deploy is running. Kept apart so the
    # process that is exposed to the network is never the one that can drop a
    # table (specification 21).
    "migration": {
        **_DATABASE_COMMON,
        "USER": os.getenv("POSTGRES_MIGRATION_USER", os.getenv("POSTGRES_USER", "finance_app")),
        "PASSWORD": os.getenv("POSTGRES_MIGRATION_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
        # A deploy is short. Holding connections open afterwards would leave the
        # privileged role connected for the life of the container.
        "CONN_MAX_AGE": 0,
    },
}

#: Both aliases point at one database, so Django must not try to create a second
#: test database or run migrations twice.
DATABASES["migration"]["TEST"] = {"MIRROR": "default"}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Seoul")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
#: Whether a write with no data key is an error rather than a plaintext store.
#: On everywhere that matters; the test settings turn it off so fixtures do not
#: all have to carry a key, and tests/test_plaintext_encryption.py turns it back
#: on to exercise the production configuration.
FIELD_ENCRYPTION_REQUIRED = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
DOCUMENT_TMP_ROOT = Path(os.getenv("DOCUMENT_TMP_ROOT", "/run/finance-ocr"))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(20 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_ENABLED = True
AXES_FAILURE_LIMIT = int(os.getenv("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = float(os.getenv("AXES_COOLOFF_HOURS", "1"))
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "3650"))
# Perceptual hashing of screenshots is optional. Disabling it leaves exact
# SHA-256 duplicate detection working unchanged.
NEAR_DUPLICATE_DETECTION_ENABLED = (
    os.getenv("NEAR_DUPLICATE_DETECTION_ENABLED", "true").strip().casefold() == "true"
)
NEAR_DUPLICATE_DISTANCE_THRESHOLD = int(os.getenv("NEAR_DUPLICATE_DISTANCE_THRESHOLD", "8"))

FIELD_ENCRYPTION_MASTER_KEY_FILE = os.getenv("FIELD_ENCRYPTION_MASTER_KEY_FILE", "")
FIELD_ENCRYPTION_MASTER_KEY_REQUIRED = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {"()": "apps.core.logging.RequestContextFilter"},
    },
    "formatters": {
        "structured": {"()": "apps.core.logging.StructuredFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_context"],
            "formatter": "structured",
        },
    },
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
