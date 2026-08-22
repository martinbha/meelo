"""Restore a master key from its separate encrypted backup."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from apps.core.key_backup import KeyBackupError, restore_key_backup

PASSPHRASE_ENV = "MEELO_KEY_BACKUP_PASSPHRASE"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="Encrypted master-key backup.")
    parser.add_argument("destination", type=Path, help="New 0600 master-key file.")
    options = parser.parse_args(argv)
    passphrase = os.environ.get(PASSPHRASE_ENV, "")
    if not passphrase:
        parser.error(f"{PASSPHRASE_ENV} must be set")
    try:
        restore_key_backup(options.backup, options.destination, passphrase=passphrase)
    except (KeyBackupError, OSError) as error:
        parser.error(str(error))
    print(f"Restored master key to {options.destination} (mode 0600).")
    print("Run the master-key verification before loading a database backup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
