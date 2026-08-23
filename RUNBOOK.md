# Runbook

How to stand this system up, run it, and get it back after something goes wrong.

This is the operator's document. It assumes one person running one instance for
themselves on one machine, because that is what the system is for. Where a topic
has depth, it lives elsewhere and is linked rather than repeated:

| Topic | Document |
| --- | --- |
| Migrations, container topology, TLS, rotation, metrics | [OPERATIONS.md](OPERATIONS.md) |
| Encryption envelope, blind indexes, threat model | [SECURITY.md](SECURITY.md) |
| Schema, ownership, constraints, roles | [DATABASE.md](DATABASE.md) |
| Parsers and fixtures | [PARSERS.md](PARSERS.md) |
| Review queue and reconciliation | [REVIEW.md](REVIEW.md) |
| Reports and exports | [REPORTS.md](REPORTS.md) |
| Categories and rules | [CATEGORIES.md](CATEGORIES.md) |

## What you are deploying

Four containers and nothing else. There is no Redis, no Celery, no message
broker, no external queue. The worker polls a PostgreSQL table with
`SELECT ... FOR UPDATE SKIP LOCKED`, which is a real queue with real
at-least-once delivery and one fewer service to operate, back up, and secure.
For one person's screenshots, a broker would be infrastructure bought with no
money down and paid for forever.

| Container | What it does |
| --- | --- |
| `proxy` | Caddy. TLS termination and the only thing on the edge network. |
| `web` | Django. Serves pages, accepts uploads, enqueues jobs. |
| `worker` | `process_document_jobs`. OCR, parsing, cleanup. No inbound ports. |
| `postgres` | The database. Not published to the host. |

`web` and `worker` share a private network with `postgres`; only `proxy` sits on
the edge. The database port is never published. See
[OPERATIONS.md](OPERATIONS.md#container-topology) for the details.

---

## First deployment

### 1. Prerequisites

Docker Engine with the Compose plugin, a domain pointed at the host, and ports
80 and 443 reachable. Nothing else. The images build from `Dockerfile`; there is
no registry to authenticate against.

### 2. Configuration

```bash
cp .env.example .env
```

Then fill it in. Every value below has no default and the application **refuses
to start** without it — deliberately, because a settings module that quietly
substitutes a working default is the one that ships signing everybody's sessions
with a key that is also in the repository.

| Variable | How to produce it |
| --- | --- |
| `DJANGO_SECRET_KEY` | `python -c 'import secrets; print(secrets.token_urlsafe(64))'` |
| `DOMAIN`, `DJANGO_ALLOWED_HOSTS` | The hostname Caddy will get a certificate for. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://` plus that hostname. |
| `POSTGRES_*_PASSWORD` | Four distinct passwords, one per role — see below. |
| `FIELD_ENCRYPTION_MASTER_KEY_SOURCE` | Path to the master key file. Next step. Optional if you use a Docker secret. |

The four PostgreSQL roles are not ceremony. The application connects as a role
that can read and write rows but cannot alter a table; migrations connect as a
role that can; backups connect as a role that can only read. A SQL injection in
the application therefore cannot drop a table, and a leaked backup credential
cannot write one. `deploy/postgres/init/10-roles.sh` creates them on first boot.
See [DATABASE.md](DATABASE.md#roles) for exactly what each one may do.

### 3. The master key

```bash
docker compose run --rm web python manage.py master_key generate --path /srv/finance/master.key
```

It writes the file with mode 0600 and refuses if something is already there —
overwriting a master key does not replace it, it makes every encrypted value in
the database permanently unreadable, and the rows survive to look fine. There is
no flag to make it not refuse. The key is never printed.

**Copy this file somewhere that is not this machine, before you put any data in
the system.** Every amount, merchant, and account name is encrypted under a
per-user key that is itself wrapped by this one. There is no recovery path, no
escrow, and no support address. Losing this file means the database is
ciphertext forever — which is the property that makes the encryption worth
having, and the reason it will not make an exception for you.

Store it in a password manager or on offline media. Do not store it in the same
backup as the database: an archive containing both the lock and its key is a
plaintext archive with extra steps. See
[OPERATIONS.md](OPERATIONS.md#what-is-deliberately-not).

`chmod 600` is not advice. The application checks the mode before it reads the
file and refuses to start if anyone but the owner can read it, naming the path
and the command to fix it.

Two other places it will be found without any configuration, if you prefer them
to a path in an environment file:

- a **Docker secret** mounted at `/run/secrets/field_encryption_master_key`,
  which keeps the key out of the image, the Compose file, and `docker inspect`;
- a **systemd credential**, if the application runs as a unit with
  `LoadCredential=field_encryption_master_key:/path/to/key`.

Setting `FIELD_ENCRYPTION_MASTER_KEY_FILE` overrides both. [SECURITY.md](SECURITY.md)
has the trade-offs.

### 3a. Check the key still opens what it opened

After a restore, before a rotation, and any time you are about to trust a
backup:

```bash
docker compose run --rm web python manage.py master_key verify
```

It unwraps every stored data key and search key and names any user whose key
will not open. A wrapped key that does not unwrap is not a warning — it is that
person's entire history — and this is the only cheap moment to find out. The
command exits non-zero if anything fails, so it can go in a cron entry.

### 4. Bring the stack up

```bash
docker compose up -d --build
```

Then apply the schema and create the account:

```bash
docker compose exec web python manage.py migrate
```

```bash
docker compose exec web python manage.py create_private_user --email you@example.com
```

It prompts for the password. Registration is closed, so this command is the only
way an account comes into existence.

`create_private_user` provisions the user's data key at the same time. A user
without a data key cannot store an amount, a merchant, or an account name, so
the two are one step — and the command reads the master key *before* it creates
anything, so a misconfigured key file fails here rather than on your first
upload.

### 5. Verify

```bash
docker compose exec web python manage.py operational_status
```

Every reading should be zero or near it, and `database_latency_ms` should be a
small number. A non-zero `cleanup_failures` on a fresh install means the shared
temporary volume is not mounted in both `web` and `worker`.

Then sign in over HTTPS and upload one screenshot. If it reaches the review
queue, the whole pipeline works: upload validation, the job table, the worker
loop, OCR, parser selection, and encryption at rest.

---

## OCR models

OCR runs inside the `worker` image; the language data ships in the image and no
model download happens at runtime. This is on purpose — a first-run download is
a first-run outage, and a model fetched over the network at runtime is a
supply-chain dependency in the middle of a private pipeline.

Korean and English are the supported languages. A screenshot in another language
raises `UnsupportedLanguageError` and is rejected at validation rather than
producing confident nonsense downstream. Adding a language means adding its data
to the image and a fixture proving the parser handles it — see
[PARSERS.md](PARSERS.md).

---

## Routine operation

The worker handles document processing on its own. These are the jobs that need
a schedule. Run them from the host's cron or a systemd timer against
`docker compose exec`.

The repository includes both scheduler formats under `deploy/maintenance/` and
`deploy/systemd/`; install one, not both. Every scheduled command goes through
`deploy/maintenance/run_command.sh`, which takes a non-blocking per-command
`flock`. An overlapping invocation logs that it was skipped. A command failure
keeps its non-zero exit code, so cron logs and systemd's journal show failures
without treating a harmless overlap skip as a failure.

For cron, create the lock and log directories for the deployment user, adjust
`/opt/finance-ocr` in the example if needed, and install the user crontab:

```bash
sudo install -d -o finance-ocr -g finance-ocr /run/lock/finance-ocr /var/log/finance-ocr
crontab deploy/maintenance/finance-ocr.cron
```

For systemd, copy the service and timer files, then enable the timers you want:

```bash
sudo install -d -o finance-ocr -g finance-ocr /run/lock/finance-ocr
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-ocr-process.timer finance-ocr-recover.timer finance-ocr-cleanup.timer
sudo systemctl enable --now finance-ocr-retention.timer finance-ocr-reconciliation.timer
sudo systemctl enable --now finance-ocr-exports.timer finance-ocr-audit.timer finance-ocr-key-verification.timer
```

The scheduled names map to the specification's maintenance operations:
`process_document_jobs --once` is the fallback for `process_queued_documents`
and dispatches `process_document`; `cleanup_document_files` removes stale
temporary directories; `expire_document_retention` deletes expired documents;
and `rotate_encryption_keys --verify-only` checks key health. The actual key
rotation remains manual because the web and worker must be stopped first. The
reconciliation timer assumes the batch `generate_reconciliation_candidates`
command from issue #235 has been deployed.

| When | Command | Why |
| --- | --- | --- |
| Every 15 min | `recover_processing_jobs` | Returns jobs orphaned by a worker that died mid-run. |
| Hourly | `cleanup_document_files` | Removes screenshot files whose rows are gone. |
| Daily | `expire_document_retention` | Enforces each document's retention policy. |
| Daily | `purge_expired_exports` | Deletes generated exports past their window. |
| Daily | `create_backup` | See below. |
| Weekly | `prune_audit_events` | Trims the audit log to its retention window. |
| Yearly | `rotate_encryption_keys` | See [OPERATIONS.md](OPERATIONS.md#key-rotation). |

A screenshot of a bank app is the most sensitive artefact in the system and the
least useful once parsed. Retention defaults to deleting it as soon as
processing finishes; the longer policies exist for when a parser is being
debugged, not as a default.

### Watching it

`operational_status --json` is the health check. The readings that matter:

- **`queue_depth` climbing and `processing` at zero** — the worker is not
  running. Check `docker compose logs worker`.
- **`failed` climbing** — parser or OCR failures. Each carries an error code;
  see the table at the end of this document.
- **`cleanup_failures` non-zero** — files are accumulating that no row points
  at. Usually a volume mount.
- **`unreviewed_observations` climbing** — not a fault. Nothing enters the books
  without a human, so this is a to-do list, not an alert.

Every reading is a number. There is no field a merchant name or a filename could
travel in, which is what makes it safe to ship these to a dashboard. See
[OPERATIONS.md](OPERATIONS.md#metrics-and-observability).

---

## Backup

```bash
docker compose exec -e MEELO_BACKUP_PASSPHRASE web python manage.py create_backup /backups/$(date +%F).enc
```

The passphrase comes from `MEELO_BACKUP_PASSPHRASE` rather than a prompt, so the
command can be scheduled. Pass it through from the host's environment as above —
writing it into a cron line puts it in a file, and writing it into the compose
file puts it in the repository.

The archive holds the database dump and the uploaded files, encrypted under a
passphrase you supply — **not** under the master key. Verify it without
restoring:

```bash
docker compose exec web python manage.py verify_backup /backups/2026-08-18.enc
```

An unverified backup is a hope. `verify_backup` checks the archive's integrity
and its manifest; it does not need the destination to be empty and does not
touch the running system.

Three things must survive independently for a restore to be possible: **the
archive**, **its passphrase**, and **the master key file**. Any two of them
without the third is not a backup. Keep them in different places.

Create the encrypted key backup from a host that can read the 0600 key file:

```bash
export MEELO_KEY_BACKUP_PASSPHRASE='...'
./scripts/backup_master_key.sh /srv/finance/master.key /secure-key-backups/meelo-master-key.backup
```

The key-backup format is independent of Django, so it can restore a missing
master key before the application starts:

```bash
./scripts/restore_master_key.sh /secure-key-backups/meelo-master-key.backup /srv/finance/master.key
```

### The restore walkthrough

For a complete disposable-database drill, follow
[docs/restore-rehearsal.md](docs/restore-rehearsal.md). The shorter sequence
below is the same restore path used for an outage.

Practise this before you need it. On a machine that is not the production host:

**1. Unpack the archive.** This does not touch a database.

```bash
MEELO_BACKUP_PASSPHRASE=... python manage.py restore_backup /backups/2026-08-18.enc /tmp/restored
```

The command reports what it unpacked: the
database fixture, the document files, and the manifest recording when the
archive was made and what version wrote it.

**2. Put the master key in place.** From the separate key backup — not from the
archive, which does not contain it.

```bash
MEELO_KEY_BACKUP_PASSPHRASE=... \
./scripts/restore_master_key.sh /secure-key-backups/meelo-master-key.backup /srv/finance/master.key
```

**3. Create the schema, then load the data.**

```bash
./scripts/restore_database.sh /backups/2026-08-18.enc /tmp/restored
```

Into an empty database. Loading a fixture over existing rows collides on
primary keys, and a partial load is worse than no load.

**4. Restore the files.** `restore_database.sh` copies the retained documents
from `/tmp/restored/documents/` to the path `DOCUMENT_TMP_ROOT` points at.

**5. Confirm it worked.** Sign in and open a report for a month you have data
for. If the figures are right, the round trip is proved end to end: the database
restored, the master key unwrapped the per-user key, and that key decrypted the
amounts. A restore verified by "the container started" is not verified.

`tests/test_end_to_end.py` walks this same path automatically on every CI run,
so the restore procedure cannot silently rot between the times you need it.

---

## Troubleshooting

Errors are classified rather than free-text, so the same failure reads the same
way in a log line, a status field, and this table.

### Upload rejected

| Error | Meaning | What to do |
| --- | --- | --- |
| `DuplicateUploadError` | This exact file was uploaded before. | Nothing. The original is already in the queue or the books. |
| `UploadValidationError` | Not an image, or fails its declared type. | Re-export the screenshot as PNG or JPEG. |
| `ImageDecodeError` | Header says image, bytes disagree. | The file is corrupt or was renamed. |
| `ImageDimensionsTooLargeError` | Beyond the decode limit. | Deliberate: a decompression bomb is a denial of service against your own machine. Crop the screenshot. |
| `UnsupportedLanguageError` | Not Korean or English. | Not supported. See OCR models above. |

### Processing failed

Transient processing errors use bounded exponential backoff: the base delay
starts at one second, doubles after each attempt, and is capped at five
minutes with small positive jitter. Each job stops after its configured
`max_attempts` (three by default). While a retry is waiting, the source
document remains `queued` and exposes both `processing_attempt_count` and
`next_processing_attempt_at`; an exhausted job leaves the document `failed`
with its final error code.

| Error | Meaning | What to do |
| --- | --- | --- |
| `OcrConfigurationError` | Language data missing from the image. | The `worker` image is wrong or stale. Rebuild it. |
| `OcrError` / `ClassifiedOcrError` | OCR could not read the image. | Usually a low-resolution or cropped screenshot. Re-take it at full width. |
| `ParserSelectionError` | No parser claims this screenshot. | The institution or screen is not supported yet. See [PARSERS.md](PARSERS.md). |
| `AmbiguousAmountError` | Two readings of the amount are equally plausible. | Correct it in the review queue. The system refuses to guess an amount. |
| `InvalidDateError` / `InvalidDateContextError` | The date could not be resolved. | Korean screenshots often omit the year. Set it in review. |
| `RetryableJobError` with a classified retryable code | Transient. | Nothing. The worker retries with backoff. |
| `NonRetryableJobError` or an unclassified code | Terminal. | Look at `last_error_code` on the job. Retrying will not help. |
| `UnsupportedTaskError` | A job names a handler that does not exist. | A worker running older code than the web container. Redeploy both. |

### Review and reconciliation

| Error | Meaning | What to do |
| --- | --- | --- |
| `ObservationActionError` | The action does not apply in this state. | Usually the row was already accepted or rejected — reload the queue. |
| `CurrencyMismatchError` | Two amounts in different currencies. | Currencies never implicitly convert. Correct the observation. |
| `ReconciliationError` | A proposed match is not valid. | See [REVIEW.md](REVIEW.md). |
| `ConflictError` | Concurrent edits to the same row. | Reload and reapply. |
| `ReprocessError` | The source file is gone. | Retention already deleted it. Re-upload the screenshot. |

### Encryption and keys

| Error | Meaning | What to do |
| --- | --- | --- |
| `KeyManagementError` | The master key is unreadable or the wrong length. | Check the path, permissions, and that the file is 32 bytes base64-encoded. |
| `InvalidCiphertextError` | Authentication tag failed. | The row is being decrypted with the wrong key, **or** it was tampered with. Do not "fix" it by re-encrypting — find out which. |
| `EncryptionError` | The user has no active data key. | `rotate_encryption_keys --email <address>`. |
| `BlindIndexError` | A search index was built with a different key. | Blind indexes are versioned and move with the key. See [SECURITY.md](SECURITY.md#blind-indexes-move-with-the-key). |
| `BackupError` | Wrong passphrase, or a corrupt archive. | `verify_backup` distinguishes the two. |

`InvalidCiphertextError` deserves emphasis. AES-GCM fails closed: it does not
return probably-right plaintext. Seeing it means the ciphertext, the key, or the
associated data is not what was used to encrypt — and the associated data binds
each value to its user, record, and field. It is the alarm working.

---

## What the MVP is

This system is finished when one person can put screenshots in and get truthful
figures out, privately, on their own machine. That is the whole scope.

### In

- Upload Korean bank and card screenshots; OCR and parse them.
- **A human confirms every row before it becomes history.** Parsed rows are
  observations; only accepted ones become transactions. This is the load-bearing
  design decision, not a workflow preference — OCR of a bank app is wrong often
  enough that automatic acceptance produces confident, wrong books.
- Double-entry ledger behind the confirmed transactions.
- Categorisation by rule, with corrections that can become rules.
- Internal transfer and refund detection, proposed for review, never automatic.
- Monthly spending, category, merchant, account, and card reports.
- CSV and JSON export.
- Every sensitive field encrypted at rest, per user.
- Exact search over encrypted fields via blind indexes.
- Encrypted backup, verified restore, key rotation.
- Single-host Compose deployment, no broker.

### Out, deliberately

Each of these was considered and left out, with a reason:

- **Bank API connections.** Korean open banking needs institutional
  registration this cannot have. Screenshots are what a person can actually get.
- **Multi-user or multi-tenant.** Ownership is enforced everywhere and tested by
  enumeration, but the product is one person's finances.
- **Automatic acceptance of high-confidence rows.** The confidence score is
  real, but the failure mode is silent and financial.
- **Currency conversion.** Storing a converted amount means storing a rate and a
  date, and getting it subtly wrong forever. Currencies stay apart.
- **Budgets, forecasts, goals.** Reporting on what happened must be right before
  predicting what will.
- **Mobile apps and a public API.** The web interface works on a phone.
- **A hosted version.** The threat model assumes you own the machine.

---

## Upgrading

```bash
git pull && docker compose up -d --build
```

```bash
docker compose exec web python manage.py migrate
```

Migrations run as the migration role, not the application role, so the
application cannot alter its own schema at runtime. Roll-forward is the expected
path; [OPERATIONS.md](OPERATIONS.md#rolling-back-an-application-migration)
covers going back when it is not.

Take a backup before the migration, not after. A backup taken after a migration
that went wrong is a backup of the problem.
