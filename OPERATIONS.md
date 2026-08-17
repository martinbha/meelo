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

## Production TLS and host checklist

Complete these checks before exposing the service outside a private development
environment:

1. Point `DOMAIN` at the deployment hostname and verify its DNS records resolve
   to the proxy.
2. Set `DJANGO_ALLOWED_HOSTS` to the exact accepted hostnames. Do not use a
   wildcard.
3. Set `DJANGO_CSRF_TRUSTED_ORIGINS` to the corresponding `https://` origins.
4. Generate a unique high-entropy `DJANGO_SECRET_KEY` and provide database
   credentials through the deployment secret store, not an environment file in
   the repository.
5. Leave `DJANGO_SECURE_SSL_REDIRECT` enabled. The Caddy proxy terminates TLS and
   forwards the original protocol through `X-Forwarded-Proto`, which Django
   trusts via `SECURE_PROXY_SSL_HEADER`.
6. Confirm ports 80 and 443 reach Caddy, certificate issuance succeeds, and the
   application is not exposed directly on port 8000.
7. Verify secure cookies, HSTS, host validation, CSRF, frame protection, and the
   referrer policy against the production settings:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production \
   uv run python manage.py check --deploy
   ```

8. Request `/health/` through the public HTTPS hostname and confirm that HTTP is
   redirected to HTTPS. Inspect `docker compose ps` to ensure every service is
   healthy.

HSTS defaults to one year and includes subdomains with preload enabled. Only use
that configuration after every subdomain is permanently available over HTTPS;
otherwise set a shorter `DJANGO_HSTS_SECONDS` during deployment validation.

## Expired exports

Generated CSV and JSON exports are plaintext copies of a user's confirmed
financial history. They expire an hour after generation and must be removed on a
schedule, because the file that matters is the one the user forgot about:

```bash
uv run python manage.py purge_expired_exports
```

Run it at least hourly alongside the other maintenance commands. The database row
survives so the audit trail keeps its shape; only the file goes.

## Key rotation

Rotate a user's data key, then verify before removing the old one:

```bash
uv run python manage.py rotate_encryption_keys --email you@example.com
uv run python manage.py rotate_encryption_keys --email you@example.com --retire
```

The first run leaves both keys in place. `--retire` removes the superseded key
only after every value has been read back under the new one. If a run is
interrupted, run it again — rotation resumes from the envelopes themselves.

To check that nothing has drifted without changing anything:

```bash
uv run python manage.py rotate_encryption_keys --verify-only
```

See [SECURITY.md](SECURITY.md) for why the ordering is what it is.
