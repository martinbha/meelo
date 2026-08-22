"""The master-key backup is separate, encrypted, and recoverable offline."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from apps.core.key_backup import (
    KeyBackupError,
    create_key_backup,
    open_key_backup,
    restore_key_backup,
)

PASSPHRASE = "a separate key backup passphrase"


def make_key(path: Path) -> bytes:
    key = os.urandom(32)
    path.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="ascii")
    path.chmod(0o600)
    return key


def test_key_backup_round_trips_without_django_startup(tmp_path: Path) -> None:
    source = tmp_path / "master.key"
    destination = tmp_path / "offline" / "master.key.backup"
    restored = tmp_path / "recovered" / "master.key"
    key = make_key(source)

    create_key_backup(source, destination, passphrase=PASSPHRASE)
    assert open_key_backup(destination, passphrase=PASSPHRASE) == key
    restore_key_backup(destination, restored, passphrase=PASSPHRASE)

    assert base64.b64decode(restored.read_text().encode(), altchars=b"-_") == key
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert key not in destination.read_bytes()


def test_key_backup_requires_the_right_passphrase(tmp_path: Path) -> None:
    source = tmp_path / "master.key"
    destination = tmp_path / "master.key.backup"
    make_key(source)
    create_key_backup(source, destination, passphrase=PASSPHRASE)

    with pytest.raises(KeyBackupError, match="wrong or"):
        open_key_backup(destination, passphrase="the wrong passphrase")


def test_key_backup_does_not_overwrite_source_or_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "master.key"
    make_key(source)

    with pytest.raises(KeyBackupError, match="separate"):
        create_key_backup(source, source, passphrase=PASSPHRASE)

    destination = tmp_path / "master.key.backup"
    create_key_backup(source, destination, passphrase=PASSPHRASE)
    with pytest.raises(KeyBackupError, match="already exists"):
        create_key_backup(source, destination, passphrase=PASSPHRASE)
