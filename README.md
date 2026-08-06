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
uv run python manage.py create_private_user --email you@example.com --superuser
```

The application processes financial screenshots locally. Do not add screenshots,
OCR output, credentials, or other sensitive data to the repository.
