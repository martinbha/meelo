#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 ARCHIVE [daily|weekly|monthly]" >&2
    exit 2
fi

archive=$1
label=${2:-daily}
case "$label" in
    daily|weekly|monthly) ;;
    *) echo "backup retention label must be daily, weekly, or monthly" >&2; exit 2 ;;
esac

: "${MEELO_BACKUP_PASSPHRASE:?MEELO_BACKUP_PASSPHRASE must be set}"
umask 077

python_command=(uv run python)
if [[ -n "${MEELO_PYTHON_COMMAND:-}" ]]; then
    read -r -a python_command <<< "$MEELO_PYTHON_COMMAND"
fi

"${python_command[@]}" manage.py create_backup "$archive" --retention-label "$label"
"${python_command[@]}" manage.py verify_backup "$archive"
echo "Verified encrypted $label database backup: $archive"
