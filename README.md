# Finance OCR

Local screenshot OCR and personal finance platform.

## Development

The project requires Python 3.12 or newer. `uv` manages the virtual environment,
development dependencies, and lockfile.

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run mypy .
uv run pre-commit install
uv run python manage.py makemigrations --check
uv run python manage.py migrate
uv run python manage.py create_private_user --email you@example.com --superuser
```

**To deploy and run this, start with [RUNBOOK.md](RUNBOOK.md)** — installation,
scheduled maintenance, a restore walkthrough, troubleshooting by error, and what
is deliberately out of scope.

Screenshot parsers, their fixtures, and the regression suite are documented in
[PARSERS.md](PARSERS.md). The review queue, reviewer actions, and
reconciliation are in [REVIEW.md](REVIEW.md). How a transaction gets a category
is in [CATEGORIES.md](CATEGORIES.md), and how a month is added up is in
[REPORTS.md](REPORTS.md). Field encryption is in [SECURITY.md](SECURITY.md), and database roles and
networking are in [DATABASE.md](DATABASE.md). Where the schema differs from the
specification, and why, is in [DATAMODEL.md](DATAMODEL.md). Operational
procedures are in [OPERATIONS.md](OPERATIONS.md).

The Compose deployment runs the web application, PostgreSQL, and the Caddy proxy
by default. The processing worker is behind a profile, so it can be run on its
own schedule or on a separate host:

```bash
docker compose --env-file .env.example up -d
```

```bash
docker compose --env-file .env.example --profile processing up -d
```

The suite runs on in-memory SQLite locally because it is fast, and again on
PostgreSQL in CI because that is what production uses — `select_for_update` is a
no-op on SQLite, and constraints and JSON lookups take different paths. Run it
against PostgreSQL yourself with:

```bash
DJANGO_SETTINGS_MODULE=config.settings.ci uv run pytest
```

Application routes follow specification section 24. `config/routes.py` holds
that table as data and `tests/test_routes.py` resolves every entry, so a path
cannot move without the check noticing — and every path that has moved keeps
answering as a permanent redirect, because a bookmark is a link somebody kept.

The application processes financial screenshots locally. Do not add screenshots,
OCR output, credentials, or other sensitive data to the repository.
