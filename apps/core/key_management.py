from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import transaction

from apps.users.models import UserDataKey

from .audit import record_audit_event
from .errors import ForbiddenError, InvalidRequestError

WRAP_FORMAT = "kw1"
KEY_SIZE = 32
#: Domain separator for the search key. Changing it invalidates every stored
#: blind index, which is a reindex rather than a deployment (#168).
BLIND_INDEX_INFO = b"finance-ocr|blind-index|v1"
NONCE_SIZE = 12
TAG_SIZE = 16


class KeyManagementError(RuntimeError):
    """Master-key loading or wrapped-key authentication failed."""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode(), altchars=b"-_", validate=True)


def _decode_master_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip().encode(), altchars=b"-_", validate=True)
    except (TypeError, ValueError) as exc:
        raise KeyManagementError("The field-encryption master key is not valid base64.") from exc
    if len(key) != KEY_SIZE:
        raise KeyManagementError("The field-encryption master key must contain 32 bytes.")
    return key


def load_master_key(path: str | Path | None = None) -> bytes:
    resolved = Path(path or settings.FIELD_ENCRYPTION_MASTER_KEY_FILE)
    if not str(resolved) or str(resolved) == ".":
        raise KeyManagementError("FIELD_ENCRYPTION_MASTER_KEY_FILE is required.")
    try:
        value = resolved.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise KeyManagementError("The field-encryption master key cannot be read.") from exc
    return _decode_master_key(value)


def derive_blind_index_key(data_key: bytes) -> bytes:
    """The search key for one user, derived from their data key.

    Kept separate from the encryption key so the two can be reasoned about
    apart: an index leak does not hand over the plaintext, and the search key
    can be rotated on its own schedule. Derivation is deterministic, so every
    caller holding the data key arrives at the same search key without a second
    secret having to be stored, wrapped, and backed up alongside the first.
    """

    if len(data_key) != KEY_SIZE:
        raise KeyManagementError("Data keys must contain exactly 32 bytes.")
    return hmac.new(data_key, BLIND_INDEX_INFO, hashlib.sha256).digest()


def _context(*, user_id: Any, version: int) -> bytes:
    return f"{WRAP_FORMAT}|users.userdatakey|{user_id}|{version}".encode()


def _assert_owner(*, user: Any, actor: Any, action: str) -> None:
    if not getattr(actor, "is_authenticated", False) or actor.pk != user.pk:
        raise ForbiddenError(f"Data-key {action} is restricted to the owning user.")


def wrap_data_key(data_key: bytes, *, master_key: bytes, user_id: Any, version: int) -> str:
    if len(data_key) != KEY_SIZE or len(master_key) != KEY_SIZE or version < 1:
        raise KeyManagementError("Data keys, master keys, and versions must be valid.")
    nonce = os.urandom(NONCE_SIZE)
    encrypted = AESGCM(master_key).encrypt(
        nonce, data_key, _context(user_id=user_id, version=version)
    )
    ciphertext, tag = encrypted[:-TAG_SIZE], encrypted[-TAG_SIZE:]
    return ".".join((WRAP_FORMAT, str(version), _encode(nonce), _encode(ciphertext), _encode(tag)))


def unwrap_data_key(envelope: str, *, master_key: bytes, user_id: Any, version: int) -> bytes:
    if len(master_key) != KEY_SIZE:
        raise KeyManagementError("The field-encryption master key must contain 32 bytes.")
    try:
        format_name, raw_version, raw_nonce, raw_ciphertext, raw_tag = envelope.split(".")
        envelope_version = int(raw_version)
        nonce, ciphertext, tag = _decode(raw_nonce), _decode(raw_ciphertext), _decode(raw_tag)
    except (TypeError, ValueError) as exc:
        raise KeyManagementError("The wrapped data-key envelope is invalid.") from exc
    if format_name != WRAP_FORMAT or envelope_version != version or len(nonce) != NONCE_SIZE:
        raise KeyManagementError("The wrapped data-key envelope is invalid.")
    try:
        key = AESGCM(master_key).decrypt(
            nonce, ciphertext + tag, _context(user_id=user_id, version=version)
        )
    except InvalidTag as exc:
        raise KeyManagementError("Wrapped data-key authentication failed.") from exc
    if len(key) != KEY_SIZE:
        raise KeyManagementError("The unwrapped data key has an invalid length.")
    return key


@transaction.atomic
def provision_user_data_key(*, user: Any, actor: Any, master_key: bytes) -> UserDataKey:
    _assert_owner(user=user, actor=actor, action="provisioning")
    locked_user = type(user).objects.select_for_update().get(pk=user.pk)
    existing = UserDataKey.objects.filter(user=locked_user, is_active=True).first()
    if existing is not None:
        return existing
    version = locked_user.encryption_key_version
    data_key = os.urandom(KEY_SIZE)
    key_record = UserDataKey.objects.create(
        user=locked_user,
        version=version,
        wrapped_key=wrap_data_key(
            data_key, master_key=master_key, user_id=locked_user.pk, version=version
        ),
    )
    record_audit_event(
        user=locked_user,
        event_type="encryption_key_provisioned",
        obj=key_record,
        metadata={"key_version": version},
    )
    return key_record


@transaction.atomic
def get_worker_data_key(*, document: Any, master_key: bytes) -> bytes:
    """Unwrap the owner's data key for a background job, with no logged-in actor.

    The worker has to decrypt: a queued screenshot is parsed minutes after the
    person who uploaded it has closed the tab, and OCR output has to be sealed
    under their key or it is not theirs. But :func:`get_user_data_key` requires
    an authenticated actor who *is* the owner, and the worker is neither.

    Passing the owner in as their own actor would satisfy that check while
    meaning nothing — the rule would be "the worker says this is fine". So this
    is a separate door with a rule of its own, and the rule is the document:

    - the key belongs to whoever owns the document being processed, and to
      nobody else. The caller does not choose the user; the document does.
    - a deactivated owner's key is not unwrapped, because a suspended account
      should stop being processed rather than quietly continue.

    The access is audited as ``worker_key_accessed`` rather than as an ordinary
    access, with the document identifier attached. That distinction matters when
    reading the log afterwards: "the owner opened their key" and "a background
    job opened the owner's key while nobody was signed in" are different events,
    and only one of them can be correlated with a person being at a keyboard.
    """

    owner = document.user
    if not owner.is_active:
        raise ForbiddenError("The document owner's account is not active.")
    key_record = UserDataKey.objects.filter(user=owner, is_active=True).first()
    if key_record is None:
        raise InvalidRequestError("No active data key exists for this user.")
    data_key = unwrap_data_key(
        key_record.wrapped_key,
        master_key=master_key,
        user_id=owner.pk,
        version=key_record.version,
    )
    record_audit_event(
        user=owner,
        event_type="worker_key_accessed",
        obj=document,
        metadata={"key_version": key_record.version, "document_id": str(document.pk)},
    )
    return data_key


@transaction.atomic
def get_user_data_key(
    *, user: Any, actor: Any, master_key: bytes, version: int | None = None
) -> bytes:
    """Unwrap one of this user's data keys — the active one unless asked otherwise.

    Rotation needs the retired version as well as the active one: it has to read
    what the old key sealed in order to write it under the new one. Retired keys
    stay unwrappable for exactly that reason, and are removed only once nothing
    references them (specification 22.6).
    """

    _assert_owner(user=user, actor=actor, action="access")
    if version is None:
        key_record = UserDataKey.objects.filter(user=user, is_active=True).first()
    else:
        key_record = UserDataKey.objects.filter(user=user, version=version).first()
    if key_record is None:
        raise InvalidRequestError("No active data key exists for this user.")
    data_key = unwrap_data_key(
        key_record.wrapped_key,
        master_key=master_key,
        user_id=user.pk,
        version=key_record.version,
    )
    record_audit_event(
        user=user,
        event_type="encryption_key_accessed",
        obj=key_record,
        metadata={"key_version": key_record.version},
    )
    return data_key
