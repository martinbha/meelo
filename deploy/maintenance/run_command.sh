#!/bin/sh

set -eu

command_name="${1:?usage: $0 MANAGEMENT_COMMAND [ARGUMENT ...]}"
shift

case "$command_name" in
    cleanup_document_files|expire_document_retention|generate_reconciliation_candidates|\
    operational_status|process_document_jobs|prune_audit_events|purge_expired_exports|\
    recover_processing_jobs|rotate_encryption_keys)
        ;;
    *)
        echo "Unsupported scheduled management command: $command_name" >&2
        exit 64
        ;;
esac

project_dir="${MEELO_PROJECT_DIR:-/opt/finance-ocr}"
lock_dir="${MEELO_MAINTENANCE_LOCK_DIR:-/run/lock/finance-ocr}"
lock_file="$lock_dir/$command_name.lock"

mkdir -p "$lock_dir"
exec 9>"$lock_file"
if ! flock -n 9; then
    echo "Skipped $command_name: another run already holds $lock_file." >&2
    exit 0
fi

cd "$project_dir"
exec docker compose exec --no-TTY web python manage.py "$command_name" "$@"
