#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
    echo "usage: $0 ARCHIVE DESTINATION [--allow-non-empty]" >&2
    exit 2
fi

archive=$1
destination=$2
allow_non_empty=${3:-}
if [[ -n "$allow_non_empty" && "$allow_non_empty" != "--allow-non-empty" ]]; then
    echo "the optional third argument must be --allow-non-empty" >&2
    exit 2
fi

: "${MEELO_BACKUP_PASSPHRASE:?MEELO_BACKUP_PASSPHRASE must be set}"
umask 077

python_command=(uv run python)
if [[ -n "${MEELO_PYTHON_COMMAND:-}" ]]; then
    read -r -a python_command <<< "$MEELO_PYTHON_COMMAND"
fi

"${python_command[@]}" manage.py verify_backup "$archive"
"${python_command[@]}" manage.py migrate --database=migration
restore_args=(manage.py restore_backup "$archive" "$destination" --load)
if [[ "$allow_non_empty" == "--allow-non-empty" ]]; then
    restore_args+=(--allow-non-empty)
fi
"${python_command[@]}" "${restore_args[@]}"
"${python_command[@]}" manage.py master_key verify
echo "Restore completed and wrapped keys verified. Keep the database archive and master key backup separate."
