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

Screenshot parsers, their fixtures, and the regression suite are documented in
[PARSERS.md](PARSERS.md). The review queue, reviewer actions, and
reconciliation are in [REVIEW.md](REVIEW.md). How a transaction gets a category
is in [CATEGORIES.md](CATEGORIES.md), and how a month is added up is in
[REPORTS.md](REPORTS.md). Operational procedures are in
[OPERATIONS.md](OPERATIONS.md).

The Compose deployment runs the web application, PostgreSQL, and Caddy proxy by
default. Enable the processing worker profile after the worker command is
implemented:

```bash
docker compose --env-file .env.example config
docker compose --env-file .env.example up -d
docker compose --env-file .env.example --profile processing up -d
```

The application processes financial screenshots locally. Do not add screenshots,
OCR output, credentials, or other sensitive data to the repository.
