# Restore rehearsal

A backup is accepted only after it has been restored into a disposable system.
Run this drill monthly and after changes to the backup format, encryption code,
migrations, or key rotation. Use the same tagged build for the rehearsal that
created the archive, or record why a newer build was chosen.

The rehearsal must have three independent inputs:

1. the encrypted database archive and `MEELO_BACKUP_PASSPHRASE`;
2. the encrypted master-key backup and `MEELO_KEY_BACKUP_PASSPHRASE`; and
3. a disposable PostgreSQL database and an empty key-file destination.

The two passphrases and the two backup files must come from different storage
locations. Never point the rehearsal at the production database, production
temporary-file volume, or the live master-key path.

## Procedure

1. Record the rehearsal date, application commit, archive path, archive
   retention label, and the destination database name. Stop if the archive is
   missing or its passphrase is not available.

2. Provision an empty PostgreSQL database in an isolated Compose project or
   disposable instance. Configure `DATABASE_ENGINE`, `POSTGRES_*`,
   `DJANGO_SETTINGS_MODULE=config.settings.production`, and a new
   `FIELD_ENCRYPTION_MASTER_KEY_FILE` for this environment. The destination
   key path must not exist yet; the restore command refuses to overwrite one.

3. Run the rehearsal wrapper from the repository root:

   ```bash
   export MEELO_BACKUP_PASSPHRASE='...'
   export MEELO_KEY_BACKUP_PASSPHRASE='...'
   export FIELD_ENCRYPTION_MASTER_KEY_FILE=/run/finance-restore/master.key
   ./scripts/rehearse_restore.sh \
     /secure-backups/meelo-2026-08-01.enc \
     /secure-key-backups/meelo-master-key.backup \
     /tmp/meelo-restore-2026-08-22
   ```

   The wrapper verifies the archive, restores the master key with mode 0600,
   migrates the empty database, loads the archive, restores retained documents,
   compares every backed-up model count with the manifest, checks wrapped keys,
   and runs `operational_status --json`. It exits non-zero on any mismatch or
   failed health check.

4. Manually inspect one known transaction from before the archive date, one
   report covering that period, and one retained document if the archive
   contains documents. The transaction must decrypt to the expected amount and
   currency, the report total must agree, and the document must open from the
   restored temporary root. Do not copy production data back into the rehearsal
   environment to make this check easier.

5. Record the output containing `RESTORE_REHEARSAL_OK`, the row-count comparison,
   the operational status result, and the three manual checks. Destroy the
   disposable database, key file, unpack directory, and temporary document root
   after recording the evidence. If any step fails, mark the backup/recovery
   drill failed and repair it before trusting a rotation or deleting an older
   backup.

## Real restore

For an outage, use the same sequence against a newly provisioned production
database, but keep the unpack directory until the application and a known
historical report have been checked:

```bash
./scripts/restore_master_key.sh /secure-key-backups/meelo-master-key.backup /srv/finance/master.key
./scripts/restore_database.sh /secure-backups/meelo-2026-08-01.enc /srv/finance/restore
```

`restore_database.sh` refuses a non-empty destination unless
`--allow-non-empty` is supplied deliberately. It verifies the archive before
migrating or loading and runs the wrapped-key check afterward.
