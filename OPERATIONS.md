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

**Stop the web and worker processes first.** The new key becomes active before
the stored values move, so a request arriving mid-rotation would try to read a row
that has not been moved yet. Rotation is fast, and the alternative ordering would
leave writes during the rotation sealed under the key being retired.

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

## Backup and restore

A backup nobody has restored is a hypothesis. These commands make testing it
cheap enough to actually do.

```bash
export MEELO_BACKUP_PASSPHRASE='...'
uv run python manage.py create_backup /backups/meelo-$(date +%F).enc
uv run python manage.py verify_backup /backups/meelo-$(date +%F).enc
```

The passphrase comes from the environment, never an argument — an argument lands
in shell history and in the process list where every other user on the machine
can read it.

### What is in the archive

Database rows, the wrapped per-user data keys, the migration state, and the
retained (already-encrypted) screenshots. The manifest records the row counts and
the schema version, so a truncated archive is caught by a count rather than by a
silent half-restore.

### What is deliberately not

**The field-encryption master key.** An archive holding both is not an encrypted
backup, it is a plaintext backup with extra steps — and the archive is the file
most likely to reach cloud storage, a laptop, or a disk somebody sells. Store the
master key where you store other credentials, and never in the same place as the
archives.

`read_manifest` refuses an archive that *claims* to contain one, because ours
never do.

### Schedule

| Frequency | Retain | Where |
| --- | --- | --- |
| Daily | 7 days | Local disk, separate volume from the database |
| Weekly | 5 weeks | Removable or remote storage |
| Monthly | 12 months | Off-site |

Run `verify_backup` on every archive as it is written. Run a full restore
rehearsal monthly:

```bash
uv run python manage.py restore_backup /backups/meelo-2026-08-01.enc /tmp/rehearsal
uv run python manage.py restore_backup /backups/meelo-2026-08-01.enc /tmp/rehearsal --load
```

Unpacking and loading are separate steps on purpose. A rehearsal should be able to
inspect what it got before replacing anything, and the first step of a real
restore is identical to the first step of a rehearsal — otherwise the rehearsal
is not testing what will happen.

### Restoring for real

1. Provision a database and run `migrate --database=migration`.
2. Put the master key in place (from wherever it is stored — **not** the archive).
3. `restore_backup <archive> <directory> --load`.
4. Confirm a transaction from before the backup reads back correctly.

Step 4 is the one that matters. Rows restored without the master key load fine and
stay unreadable, which looks like success until somebody opens a report.

## Metrics and observability

```bash
uv run python manage.py operational_status
uv run python manage.py operational_status --json
uv run python manage.py operational_status --emit-metrics
```

Reports queue depth, in-flight documents, failures, cleanup failures, unreviewed
observations, and database latency. Queue depth counts what has **not started** —
a queue that looks empty because everything is mid-OCR is not an empty queue.

### What a metric may carry

Operational metrics and financial privacy pull in opposite directions: the useful
label is always the specific one, and the specific one is what turns a metrics
pipeline into an unencrypted copy of somebody's finances, sitting wherever metrics
go and outliving the database's retention.

`apps.core.metrics` refuses those labels rather than asking callers to avoid them:

- **Metric names come from a fixed list.** One added ad hoc is one nobody has
  checked.
- **Label names come from an allow-list** — identifiers, statuses, parser names.
  So `merchant` cannot arrive by being spelled slightly differently.
- **Label values must look like identifiers.** Anything with a space, a currency
  symbol, or non-Latin text is refused, because that is how a merchant name or an
  amount would arrive.

The structured log formatter redacts sensitive keys as well, so a value that got
past the allow-list still does not reach a log line.

### Correlating a failure

Every log line and every metric carries `request_id` and `task_id`. The worker
sets the task identifier for each job it picks up, so a failure there can be
joined to the request that queued the work. Without that pair, "why did this
document never finish" is answered by reading timestamps and guessing.

### Optional error reporting

If an error-reporting service is ever added, it must follow the same rules and one
more:

1. **No request bodies, no form data, no query strings.** An upload body is a
   screenshot of somebody's bank account.
2. **No local variables in stack frames.** That is where decrypted amounts and
   merchant names live at the moment something raises.
3. **Scrub by allow-list, not by denylist.** Send the exception type, the module,
   the line, the request id, and the task id. Nothing else.
4. **Self-hosted, or not at all.** Sending financial stack traces to a third party
   is a decision the deployment's owner makes deliberately, not a default.

Until such an integration exists, the structured logs are the record, and they
stay on the machine that produced them.
