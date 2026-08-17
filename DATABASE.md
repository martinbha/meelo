# Database Access

Four roles, because the process exposed to the network should not be the one
that can drop a table.

| Role | Used by | Can |
| --- | --- | --- |
| `finance_owner` | The init script, once | Everything. Never used again after first start |
| `finance_migrate` | `manage.py migrate`, during a deploy | Own and change the schema |
| `finance_app` | The web and worker processes | `SELECT`, `INSERT`, `UPDATE`, `DELETE` on rows |
| `finance_backup` | `pg_dump` | `SELECT`, and nothing else |

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

## Row-level security: evaluated, not enabled

PostgreSQL RLS would move the ownership check into the database, so a missing
`WHERE user_id` could not leak a row. It is **not** enabled, for three reasons:

1. **It needs a per-request session variable.** `SET LOCAL app.user_id` on every
   connection, correct under connection pooling, or the policy either blocks
   everything or allows everything. That is a new invariant which, if it broke,
   would fail *open*.
2. **This is a single-user deployment.** The blast radius RLS reduces —
   one user reading another's rows — is a radius of one.
3. **The checks it would duplicate are already tested.** Ownership is asserted
   on every read path, so RLS would be a second implementation of a rule that
   already has one.

Enable it when the deployment stops being single-user. The migration would add
`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` plus a policy per table on
`user_id = current_setting('app.user_id')::uuid`, and the session variable would
have to be set in middleware and asserted in a test that fails when it is absent.

## Testing

```bash
uv run pytest tests/test_compose.py tests/test_production_security.py
```
