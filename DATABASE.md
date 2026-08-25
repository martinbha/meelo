# Database Access

Four runtime roles, because the process exposed to the network should not be
the one that can drop a table.

| Role | Used by | Can |
| --- | --- | --- |
| `finance_owner` | The init script, once | Everything. Never used again after first start |
| `finance_migrate` | `manage.py migrate`, during a deploy | Own and change the schema |
| `finance_app` | The web and worker processes | `SELECT`, `INSERT`, `UPDATE`, `DELETE` on rows |
| `finance_backup` | `pg_dump` | `SELECT`, and nothing else |
| `finance_readonly` | Reporting and verification jobs | `SELECT`, and nothing else |

The split is about blast radius. `finance_app` runs continuously and is reachable
from the proxy, so it gets the narrowest grant that still lets the application
work: rows in, rows out. It cannot create a table, drop one, read another
database, or grant itself more — so a flaw in the application is a flaw in the
application, not in the schema.

`deploy/postgres/init/10-roles.sh` creates the roles on first start. It is
idempotent, so a restart over an existing volume does nothing. `ALTER DEFAULT
PRIVILEGES` covers whatever the *next* migration creates, so a new table is
neither unreadable by the application nor readable by everyone.

## Two aliases, one database

`DATABASES["default"]` connects as `finance_app`. `DATABASES["migration"]`
connects as `finance_migrate` and is used only for schema changes:

```bash
uv run python manage.py migrate --database=migration
```

`CONN_MAX_AGE` is 0 on that alias: a deploy is short, and the privileged role
should not stay connected for the life of the container. In tests the alias
mirrors `default`, because it is one database reached two ways rather than two
databases.

## Rotating role passwords

The role passwords are independent. Rotate one role at a time during a planned
maintenance window:

1. Change the role password from an owner session without putting the new value
   in shell history or a command-line argument:

   ```text
   docker compose exec postgres psql -U finance_owner -d finance_ocr
   \password finance_app
   ```

   Replace `finance_app` with the role being rotated. The owner session is only
   used for this administrative action; no application process uses it.
2. Update the matching secret (`POSTGRES_APP_PASSWORD`,
   `POSTGRES_MIGRATION_PASSWORD`, `POSTGRES_BACKUP_PASSWORD`, or
   `POSTGRES_READONLY_PASSWORD`) in the deployment secret store.
3. Recreate the process that uses the role so new connections receive the new
   password. For the application role, recreate both `web` and `worker`; the
   migration, backup, and read-only jobs should be restarted before their next
   run.
4. Verify the service health check and a connection using the rotated role
   before ending the maintenance window.

The init script creates missing roles on first boot, but it is not a password
rotation mechanism and is not rerun for an existing PostgreSQL volume.

## The database is not on the network

There is no `ports:` entry on the `postgres` service, and the service sits on an
`internal: true` Docker network. That means it is unreachable from the host — a
published port is one firewall mistake away from the internet, and a personal
finance database is exactly the thing that should not be one mistake away.

Only the proxy is on the `edge` network. For maintenance:

```bash
docker compose exec postgres psql -U finance_owner -d finance_ocr
```

Every role password is `${...:?}` in Compose, so the stack refuses to start
without one rather than falling back to a default somebody keeps.

## Ownership is enforced in the application

Every query for user-owned data goes through `apps.core.ownership.owned_queryset`,
which filters on `user_id`. Views resolve objects with `get_owned_object_or_404`,
so another user's row is a 404 rather than a permission error — they have no
business learning it exists.

Cross-table ownership that a constraint cannot express is enforced at the model:
`ReconciliationMatch.save` refuses a match joining two people's rows, because a
database `CHECK` cannot reach another table.

`tests/test_security.py` and the isolation tests in each feature's suite assert
this for every read path.

## Row-level security decision: defer

PostgreSQL RLS would make a missing application `WHERE user_id` fail closed.
The prototype policy for a directly owned table is:

```sql
USING (
    user_id = NULLIF(current_setting('meelo.user_id', true), '')::bigint
)
WITH CHECK (
    user_id = NULLIF(current_setting('meelo.user_id', true), '')::bigint
)
```

The `true` argument makes an unset variable return `NULL`; SQL comparison with
`NULL` is not true, so a query without a user scope sees no owned rows. The user
key is a database integer, not a UUID. An authenticated operation would have to
open a transaction and run `SET LOCAL meelo.user_id = '<id>'` before any owned
query. `SET LOCAL` is necessary: a session-level value can leak between requests
when Django reuses a connection.

RLS is deliberately **not enabled** in this single-user release. A runtime
feature flag is not a safe schema switch: migrations would produce different
database state from the same migration history, and the application role cannot
alter policies. Enabling it therefore needs an explicit, reversible migration
when the deployment becomes multi-user.

### Prototype impact

The schema contains 20 tables with a direct `user_id`. Applying one policy shape
to those tables is straightforward, but it is not the complete ownership model:

- `ledger_ledgerentry` derives ownership through its transaction and needs an
  `EXISTS` policy or a denormalized owner column. The former adds a join to every
  ledger read; the latter creates another ownership invariant.
- Authentication, session, migration, and operational tables are intentionally
  not user-scoped. A blanket policy generator would block login or worker health
  before an identity exists.
- Web requests would need one transaction spanning authentication through the
  response query. Reports would keep that transaction open while decrypting and
  aggregating rows.
- Workers would need one user-scoped transaction per claimed job. Queue claiming
  itself must remain unscoped so a worker can discover the next owner, then enter
  that owner's scope before loading pipeline data.
- Maintenance commands that operate across users would require a separately
  audited bypass role or explicit iteration through user-scoped transactions.
  The migration role already owns the tables and can manage policies, while the
  application role cannot alter schema.

This operational surface is disproportionate while a supported deployment has
one user, and application ownership filters plus isolation tests already cover
the same read paths. Revisit the decision before multi-user support. At that
point, add explicit policy migrations, PostgreSQL integration tests proving an
unset scope returns no rows, worker and maintenance tests under forced RLS, and
report query-plan measurements on representative data.

## Testing

```bash
uv run pytest tests/test_compose.py tests/test_production_security.py
```

## Roles

`deploy/postgres/init/10-roles.sh` creates four login roles on first boot, none
of which is the superuser the container starts with. `PUBLIC` is stripped of
everything on both the database and the `public` schema first, so a role has
exactly what it was granted and nothing inherited.

| Role | Can | Cannot |
| --- | --- | --- |
| `finance_migrate` | Everything on the schema — create, alter, drop. | — |
| `finance_app` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` on tables; use sequences. | Alter or drop anything. |
| `finance_backup` | `SELECT`. | Write anything at all. |
| `finance_readonly` | `SELECT`. | Write anything at all, or use sequences. |

The split is what turns two whole classes of failure into smaller ones. A SQL
injection reaching the application's connection cannot drop a table, because
that connection has never had the privilege to. A leaked backup credential — the
one most likely to end up on another machine, in a cron line, or in a log —
cannot modify a single row.

Default privileges are granted as well as current ones, so a table created by a
future migration is reachable by the application without anybody remembering to
re-grant. A permission model that needs a manual step after every migration is
one that will be fixed by granting too much.

Passwords reach `psql` through `--set` and are interpolated with `format(%L)`
rather than pasted into the SQL text. A password containing an apostrophe is
otherwise a syntax error at best and an injection at worst.
