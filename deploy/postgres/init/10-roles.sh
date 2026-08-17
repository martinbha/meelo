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
#
# Roles are created idempotently so a container restart over an existing volume
# is a no-op rather than a failure.

set -eu

: "${POSTGRES_DB:?POSTGRES_DB must be set}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set}"
: "${POSTGRES_MIGRATION_PASSWORD:?POSTGRES_MIGRATION_PASSWORD must be set}"
: "${POSTGRES_BACKUP_PASSWORD:?POSTGRES_BACKUP_PASSWORD must be set}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_migrate') THEN
        CREATE ROLE finance_migrate LOGIN PASSWORD '${POSTGRES_MIGRATION_PASSWORD}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_app') THEN
        CREATE ROLE finance_app LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finance_backup') THEN
        CREATE ROLE finance_backup LOGIN PASSWORD '${POSTGRES_BACKUP_PASSWORD}';
    END IF;
END
\$\$;

-- Nobody connects to anything they were not given.
REVOKE ALL ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO finance_migrate, finance_app, finance_backup;

-- The schema belongs to the migration role, so a deploy can change it and the
-- application cannot.
ALTER SCHEMA public OWNER TO finance_migrate;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO finance_app, finance_backup;
GRANT ALL ON SCHEMA public TO finance_migrate;

-- Rows in, rows out, nothing structural.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finance_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO finance_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_backup;

-- And the same for whatever the next migration creates, so a new table is not
-- accidentally readable by everyone or unreadable by the application.
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrate IN SCHEMA public
    GRANT SELECT ON TABLES TO finance_backup;
SQL
