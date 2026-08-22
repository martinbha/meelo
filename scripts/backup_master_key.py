"""Create an encrypted backup of the master key, independent of the database."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from apps.core.key_backup import KeyBackupError, create_key_backup

PASSPHRASE_ENV = "MEELO_KEY_BACKUP_PASSPHRASE"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing 0600 master-key file.")
    parser.add_argument("destination", type=Path, help="Separate encrypted key-backup file.")
    options = parser.parse_args(argv)
    passphrase = os.environ.get(PASSPHRASE_ENV, "")
    if not passphrase:
        parser.error(f"{PASSPHRASE_ENV} must be set")
    try:
        create_key_backup(options.source, options.destination, passphrase=passphrase)
    except (KeyBackupError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote encrypted master-key backup to {options.destination} (mode 0600).")
    print("Keep this file and its passphrase separate from database backups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
