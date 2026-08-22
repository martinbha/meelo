#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 ARCHIVE KEY_BACKUP UNPACK_DIRECTORY" >&2
    exit 2
fi

: "${MEELO_BACKUP_PASSPHRASE:?MEELO_BACKUP_PASSPHRASE must be set}"
: "${MEELO_KEY_BACKUP_PASSPHRASE:?MEELO_KEY_BACKUP_PASSPHRASE must be set}"
: "${FIELD_ENCRYPTION_MASTER_KEY_FILE:?FIELD_ENCRYPTION_MASTER_KEY_FILE must point to a new rehearsal key path}"
umask 077

python_command=(uv run python)
if [[ -n "${MEELO_PYTHON_COMMAND:-}" ]]; then
    read -r -a python_command <<< "$MEELO_PYTHON_COMMAND"
fi

"${python_command[@]}" scripts/restore_master_key.py "$2" "$FIELD_ENCRYPTION_MASTER_KEY_FILE"
./scripts/restore_database.sh "$1" "$3"
"${python_command[@]}" scripts/check_restore_rehearsal.py "$3"
"${python_command[@]}" manage.py operational_status --json
echo "RESTORE_REHEARSAL_OK"
