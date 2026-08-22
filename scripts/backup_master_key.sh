#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 SOURCE_KEY DESTINATION" >&2
    exit 2
fi

: "${MEELO_KEY_BACKUP_PASSPHRASE:?MEELO_KEY_BACKUP_PASSPHRASE must be set}"
umask 077
uv run python scripts/backup_master_key.py "$1" "$2"
