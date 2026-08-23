#!/bin/sh
# Run the backup/restore integration suite against a disposable PostgreSQL.

set -eu

test_db="${POSTGRES_TEST_DB:-finance_ocr_backup_ci}"
test_user="${POSTGRES_TEST_USER:-finance_backup_ci}"
test_password="${POSTGRES_TEST_PASSWORD:-finance_backup_ci}"
test_host="${POSTGRES_TEST_HOST:-127.0.0.1}"
test_port="${POSTGRES_TEST_PORT:-55432}"
test_image="${POSTGRES_TEST_IMAGE:-postgres:17-alpine}"
container_name="meelo-postgres-backup-tests-$$"

cleanup() {
    docker rm --force "$container_name" >/dev/null 2>&1 || true
}

trap cleanup EXIT HUP INT TERM

docker run --detach \
    --name "$container_name" \
    --publish "${test_host}:${test_port}:5432" \
    --env "POSTGRES_DB=$test_db" \
    --env "POSTGRES_USER=$test_user" \
    --env "POSTGRES_PASSWORD=$test_password" \
    "$test_image" >/dev/null

attempt=0
until docker exec "$container_name" pg_isready --username "$test_user" --dbname "$test_db" \
    >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "PostgreSQL did not become ready within 60 seconds." >&2
        exit 1
    fi
    sleep 1
done

export DJANGO_SETTINGS_MODULE=config.settings.ci
export POSTGRES_TEST_DB="$test_db"
export POSTGRES_TEST_USER="$test_user"
export POSTGRES_TEST_PASSWORD="$test_password"
export POSTGRES_TEST_HOST="$test_host"
export POSTGRES_TEST_PORT="$test_port"

uv run python manage.py migrate
uv run pytest tests/test_backup_restore.py -v
