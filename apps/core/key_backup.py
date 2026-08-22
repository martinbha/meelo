"""Encrypt and restore the field-encryption master key separately.

This module intentionally has no Django imports. A database restore can only
open encrypted rows after the master key is back, and the application is
configured to refuse startup without that key. Recovery must therefore remain
usable while the application is down.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BACKUP_FORMAT = "meelo-master-key-backup-v1"
KEY_SIZE = 32
KEY_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
FORBIDDEN_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO
_SALT_SIZE = 16
_NONCE_SIZE = 12
_TIME_COST = 3
_MEMORY_COST = 64 * 1024
_PARALLELISM = 4
MINIMUM_PASSPHRASE_LENGTH = 12


class KeyBackupError(RuntimeError):
    """A key backup cannot be created, opened, or restored."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < MINIMUM_PASSPHRASE_LENGTH:
        raise KeyBackupError(
            f"The key-backup passphrase must be at least {MINIMUM_PASSPHRASE_LENGTH} characters."
        )
    return hash_secret_raw(
        secret=passphrase.encode(),
        salt=salt,
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST,
        parallelism=_PARALLELISM,
        hash_len=KEY_SIZE,
        type=Argon2Type.ID,
    )


def _read_key(source: Path) -> bytes:
    try:
        mode = stat.S_IMODE(source.stat().st_mode)
    except OSError as error:
        raise KeyBackupError(f"The master key at {source} cannot be inspected.") from error
    if mode & FORBIDDEN_MODE_BITS:
        raise KeyBackupError(
            f"The master key at {source} is readable beyond its owner (mode {mode:04o})."
        )
    try:
        encoded = source.read_text(encoding="ascii").strip()
        key = base64.b64decode(encoded.encode(), altchars=b"-_", validate=True)
    except (OSError, UnicodeError, ValueError) as error:
        raise KeyBackupError(f"The master key at {source} is not valid base64.") from error
    if len(key) != KEY_SIZE:
        raise KeyBackupError(f"The master key at {source} must contain 32 bytes.")
    return key


def _assert_distinct(source: Path, destination: Path) -> None:
    try:
        same_file = source.resolve() == destination.resolve()
    except OSError as error:
        raise KeyBackupError("The key backup paths could not be compared.") from error
    if same_file:
        raise KeyBackupError("The key backup destination must be separate from the source key.")


def create_key_backup(source: Path, destination: Path, *, passphrase: str) -> None:
    """Seal the key to a separate file without printing key material."""

    source = source.expanduser()
    destination = destination.expanduser()
    _assert_distinct(source, destination)
    if destination.exists():
        raise KeyBackupError(f"The key backup destination already exists: {destination}.")
    key = _read_key(source)
    salt = os.urandom(_SALT_SIZE)
    nonce = os.urandom(_NONCE_SIZE)
    header = KEY_BACKUP_FORMAT.encode() + b"\n"
    payload = json.dumps(
        {
            "format": KEY_BACKUP_FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "key": base64.urlsafe_b64encode(key).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("ascii")
    sealed = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, payload, header)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        os.write(descriptor, header + salt + nonce + sealed)
    finally:
        os.close(descriptor)


def open_key_backup(path: Path, *, passphrase: str) -> bytes:
    """Open and validate a key backup, returning the raw key only to callers."""

    header = KEY_BACKUP_FORMAT.encode() + b"\n"
    try:
        blob = path.read_bytes()
    except OSError as error:
        raise KeyBackupError(f"The key backup at {path} cannot be read.") from error
    if not blob.startswith(header):
        raise KeyBackupError("This file is not a field-encryption key backup.")
    body = blob[len(header) :]
    if len(body) <= _SALT_SIZE + _NONCE_SIZE:
        raise KeyBackupError("The field-encryption key backup is truncated.")
    salt = body[:_SALT_SIZE]
    nonce = body[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
    try:
        payload = AESGCM(_derive_key(passphrase, salt)).decrypt(
            nonce, body[_SALT_SIZE + _NONCE_SIZE :], header
        )
        decoded = json.loads(payload)
        if decoded.get("format") != KEY_BACKUP_FORMAT:
            raise KeyBackupError("The field-encryption key backup format is unknown.")
        key = base64.b64decode(decoded["key"].encode(), altchars=b"-_", validate=True)
    except (InvalidTag, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise KeyBackupError("The passphrase is wrong or the key backup is damaged.") from error
    if len(key) != KEY_SIZE:
        raise KeyBackupError("The field-encryption key backup does not contain a 32-byte key.")
    return key


def restore_key_backup(backup: Path, destination: Path, *, passphrase: str) -> None:
    """Write a recovered key with mode 0600, refusing to overwrite one."""

    _assert_distinct(backup, destination)
    if destination.exists():
        raise KeyBackupError(
            f"The restore destination already exists: {destination}. Refusing to overwrite it."
        )
    key = open_key_backup(backup, passphrase=passphrase)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        os.write(descriptor, base64.urlsafe_b64encode(key))
    finally:
        os.close(descriptor)
