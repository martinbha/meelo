# Operations Guide

## Database migrations

Run all migration commands from the repository root with the intended Django
settings module and database environment configured.

Preview pending migrations before applying them:

```bash
uv run python manage.py showmigrations --plan
uv run python manage.py migrate --plan
```

Apply the complete migration history to a new or existing database:

```bash
uv run python manage.py migrate
```

Verify that model changes have a committed migration:

```bash
uv run python manage.py makemigrations --check --dry-run
```

### Rolling back an application migration

Back up the database before a production rollback. Identify the preceding
migration with `showmigrations`, inspect the reverse plan, and migrate the
application to that explicit target:

```bash
uv run python manage.py showmigrations <application>
uv run python manage.py migrate <application> <previous_migration> --plan
uv run python manage.py migrate <application> <previous_migration>
```

Use `zero` only when intentionally removing every migration for one application:

```bash
uv run python manage.py migrate <application> zero --plan
uv run python manage.py migrate <application> zero
```

Never delete migration files to roll back a deployed database. If a migration
cannot be reversed safely, restore the database backup or ship a new forward
migration that repairs the schema and data.

### Fresh-database verification

The test settings create an isolated database for every run. Use the migration
test and the complete test suite to verify that the migration history can build
a database repeatedly from zero:

```bash
uv run pytest tests/test_migrations.py
uv run pytest
```

## Container topology

The Compose deployment contains four Redis-free services:

- `web` serves Django through Gunicorn.
- `worker` claims processing jobs directly from PostgreSQL.
- `postgres` stores application and queue state.
- `proxy` terminates TLS and forwards requests to `web`.

Start the web, database, and proxy services with:

```bash
docker compose up -d
```

Enable the database-backed processing worker with:

```bash
docker compose --profile processing up -d
```

Inspect readiness before sending traffic or processing documents:

```bash
docker compose ps
docker compose --profile processing ps
```

### Shared temporary screenshot storage

The `finance_ocr_tmp` volume is mounted at `/run/finance-ocr` in both `web` and
`worker`. Upload requests write private per-document directories into that
mount, and the worker reads and removes the same files during processing. The
volume is backed by an in-memory `tmpfs`; it is not a repository path or public
media directory and is discarded when the Docker volume is removed.

Do not replace the two mounts with separate volumes. Both processes must resolve
`DOCUMENT_TMP_ROOT` to the same private storage namespace. Directory and file
permissions remain restricted to `0700` and `0600`, respectively.
