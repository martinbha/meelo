#!/bin/sh
# Create the four database roles this deployment uses.
#
# The point of the split is blast radius. The web process runs continuously and
# is the thing exposed to the network, so it gets the narrowest grant that still
# lets the application work: read and write rows, and nothing else. It cannot
# create a table, drop one, read another database, or grant itself more.
#
#   finance_owner    the bootstrap superuser. Used by this script and never again.
#   finance_migrate  owns the schema. DDL only, and only when a deploy runs.
#   finance_app      the web and worker processes. SELECT/INSERT/UPDATE/DELETE.
#   finance_backup   pg_dump. SELECT on everything, write on nothing.
#   finance_readonly reporting or verification. SELECT on everything, write on nothing.
#
# Roles are created idempotently so a container restart over an existing volume
# is a no-op rather than a failure.

set -eu

: "${POSTGRES_DB:?POSTGRES_DB must be set}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set}"
: "${POSTGRES_MIGRATION_PASSWORD:?POSTGRES_MIGRATION_PASSWORD must be set}"
: "${POSTGRES_BACKUP_PASSWORD:?POSTGRES_BACKUP_PASSWORD must be set}"
: "${POSTGRES_READONLY_PASSWORD:?POSTGRES_READONLY_PASSWORD must be set}"

# Passwords are passed as psql variables and quoted with :'name', which escapes
# them properly. Interpolating them into the SQL text with the shell would break
# on an apostrophe and would accept one as SQL.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set ON_ERROR_STOP=1 \
    --set app_password="$POSTGRES_APP_PASSWORD" \
    --set migration_password="$POSTGRES_MIGRATION_PASSWORD" \
    --set backup_password="$POSTGRES_BACKUP_PASSWORD" \
    --set readonly_password="$POSTGRES_READONLY_PASSWORD" <<SQL
SELECT format('CREATE ROLE finance_migrate LOGIN PASSWORD %L', :'migration_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_migrate') \gexec
SELECT format('CREATE ROLE finance_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_app') \gexec
SELECT format('CREATE ROLE finance_backup LOGIN PASSWORD %L', :'backup_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_backup') \gexec
SELECT format('CREATE ROLE finance_readonly LOGIN PASSWORD %L', :'readonly_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_readonly') \gexec

-- Nobody connects to anything they were not given.
REVOKE ALL ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB}
    TO finance_migrate, finance_app, finance_backup, finance_readonly;

-- The schema belongs to the migration role, so a deploy can change it and the
-- application cannot.
ALTER SCHEMA public OWNER TO finance_migrate;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO finance_app, finance_backup, finance_readonly;
GRANT ALL ON SCHEMA public TO finance_migrate;

-- Rows in, rows out, nothing structural.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finance_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finance_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_backup;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM finance_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_readonly;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM finance_readonly;

-- And the same for whatever the next migration creates, so a new table is not
-- accidentally readable by everyone or unreadable by the application.
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT SELECT ON TABLES TO finance_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT SELECT ON TABLES TO finance_readonly;
SQL
