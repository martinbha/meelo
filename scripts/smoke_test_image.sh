#!/bin/sh
# Check that a built application image can render HTML and serve its assets.

set -eu

image="${1:?usage: $0 IMAGE[:TAG]}"
host_port="${IMAGE_SMOKE_PORT:-18080}"
container_name="meelo-image-smoke-$$"
base_url="http://127.0.0.1:${host_port}"

cleanup() {
    docker rm --force "$container_name" >/dev/null 2>&1 || true
}

trap cleanup EXIT HUP INT TERM

docker run --detach \
    --name "$container_name" \
    --publish "127.0.0.1:${host_port}:8000" \
    --env DJANGO_SETTINGS_MODULE=config.settings.production \
    --env DJANGO_SECRET_KEY=image-smoke-test-secret \
    --env DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost \
    --env FIELD_ENCRYPTION_MASTER_KEY_FILE=/tmp/image-smoke-master-key \
    --entrypoint /bin/sh \
    "$image" \
    -c 'umask 077; python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > "$FIELD_ENCRYPTION_MASTER_KEY_FILE"; exec gunicorn config.wsgi:application --bind 0.0.0.0:8000' \
    >/dev/null

attempt=0
until curl --fail --silent --show-error "$base_url/login/" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "The application image did not serve /login/ within 60 seconds." >&2
        exit 1
    fi
    sleep 1
done

login_html="$(curl --fail --silent --show-error "$base_url/login/")"
printf '%s\n' "$login_html" | grep -F "Sign in" >/dev/null

static_path="$(printf '%s\n' "$login_html" | sed -n 's|.*href="\([^\"]*css/app[^\"]*\)".*|\1|p' | head -n 1)"
if [ -z "$static_path" ]; then
    echo "The login page did not reference the application stylesheet." >&2
    exit 1
fi
case "$static_path" in
    /*) ;;
    *) static_path="/$static_path" ;;
esac

curl --fail --silent --show-error "$base_url$static_path" >/dev/null
