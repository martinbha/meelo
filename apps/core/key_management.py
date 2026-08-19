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

from apps.users.models import UserDataKey, UserSearchKey

from .audit import record_audit_event
from .errors import ForbiddenError, InvalidRequestError

WRAP_FORMAT = "kw1"
KEY_SIZE = 32
#: Domain separator for the search key. Distinct from anything the data key is
#: used for, so the two derivations cannot produce the same bytes. Changing it
#: invalidates every stored blind index, which is a reindex rather than a
#: deployment (#168).
SEARCH_KEY_INFO = b"finance-ocr|search-key|v1"
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


def derive_search_key(*, master_key: bytes, user_id: Any, version: int) -> bytes:
    """One user's blind-index key, derived from the master key.

    Derived from the *master* key rather than from the data key, and that is the
    whole design. A search key derived from the data key means the two are one
    secret wearing two hats: anyone who reaches the plaintext can also build
    search tokens, and can then confirm guesses against every index in the
    database — including rows they could not otherwise read.

    The label is its own, so the two derivations cannot collide, and the version
    is inside the derivation, so rotating the search key produces a genuinely
    different key rather than the same bytes under a new number
    (specification 22.4).
    """

    if len(master_key) != KEY_SIZE:
        raise KeyManagementError("The field-encryption master key must contain 32 bytes.")
    if version < 1:
        raise KeyManagementError("Search key versions start at one.")
    info = b"|".join((SEARCH_KEY_INFO, str(user_id).encode(), str(version).encode()))
    return hmac.new(master_key, info, hashlib.sha256).digest()


def _context(*, user_id: Any, version: int, model: str = "users.userdatakey") -> bytes:
    return f"{WRAP_FORMAT}|{model}|{user_id}|{version}".encode()


def _assert_owner(*, user: Any, actor: Any, action: str) -> None:
    if not getattr(actor, "is_authenticated", False) or actor.pk != user.pk:
        raise ForbiddenError(f"Data-key {action} is restricted to the owning user.")


def wrap_data_key(
    data_key: bytes,
    *,
    master_key: bytes,
    user_id: Any,
    version: int,
    model: str = "users.userdatakey",
) -> str:
    if len(data_key) != KEY_SIZE or len(master_key) != KEY_SIZE or version < 1:
        raise KeyManagementError("Data keys, master keys, and versions must be valid.")
    nonce = os.urandom(NONCE_SIZE)
    encrypted = AESGCM(master_key).encrypt(
        nonce, data_key, _context(user_id=user_id, version=version, model=model)
    )
    ciphertext, tag = encrypted[:-TAG_SIZE], encrypted[-TAG_SIZE:]
    return ".".join((WRAP_FORMAT, str(version), _encode(nonce), _encode(ciphertext), _encode(tag)))


def unwrap_data_key(
    envelope: str,
    *,
    master_key: bytes,
    user_id: Any,
    version: int,
    model: str = "users.userdatakey",
) -> bytes:
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
            nonce, ciphertext + tag, _context(user_id=user_id, version=version, model=model)
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
    # The two keys are provisioned together because a user needs both from the
    # first write: a value encrypted with no way to index it is a value nothing
    # can ever look up. They are still separate keys, separately versioned, and
    # separately rotatable — provisioned together is not the same as derived
    # from each other.
    provision_user_search_key(user=locked_user, actor=actor, master_key=master_key)
    return key_record


SEARCH_KEY_MODEL = "users.usersearchkey"


@transaction.atomic
def provision_user_search_key(*, user: Any, actor: Any, master_key: bytes) -> UserSearchKey:
    """Give a user their blind-index key, once.

    Stored wrapped even though it is derived deterministically. Deriving it on
    demand would work, and it would also mean the search key had no version of
    its own to rotate and no row to retire — rotation would be a code change
    rather than an operation. A stored row makes the key a thing with a
    lifecycle, which is what #168 needs.
    """

    _assert_owner(user=user, actor=actor, action="provisioning")
    locked_user = type(user).objects.select_for_update().get(pk=user.pk)
    existing = UserSearchKey.objects.filter(user=locked_user, is_active=True).first()
    if existing is not None:
        return existing
    version = 1
    search_key = derive_search_key(master_key=master_key, user_id=locked_user.pk, version=version)
    record = UserSearchKey.objects.create(
        user=locked_user,
        version=version,
        wrapped_key=wrap_data_key(
            search_key,
            master_key=master_key,
            user_id=locked_user.pk,
            version=version,
            model=SEARCH_KEY_MODEL,
        ),
    )
    record_audit_event(
        user=locked_user,
        event_type="search_key_provisioned",
        obj=record,
        metadata={"search_key_version": version},
    )
    return record


def unwrap_search_key(record: UserSearchKey, *, master_key: bytes) -> bytes:
    """Open one stored search key.

    Bound to its own model name in the associated data, so a wrapped *data* key
    pasted into this column fails to open rather than becoming a search key —
    which would quietly reunite the two secrets this separation exists to keep
    apart.
    """

    return unwrap_data_key(
        record.wrapped_key,
        master_key=master_key,
        user_id=record.user_id,
        version=record.version,
        model=SEARCH_KEY_MODEL,
    )


@transaction.atomic
def get_user_search_key(*, user: Any, actor: Any, master_key: bytes) -> bytes:
    """The active search key for one user, for an actor entitled to it."""

    _assert_owner(user=user, actor=actor, action="access")
    record = UserSearchKey.objects.filter(user=user, is_active=True).first()
    if record is None:
        raise InvalidRequestError("No active search key exists for this user.")
    return unwrap_search_key(record, master_key=master_key)


@transaction.atomic
def get_worker_search_key(*, document: Any, master_key: bytes) -> bytes:
    """The document owner's search key, for a job with no logged-in actor.

    The same rule as :func:`get_worker_data_key`: the document decides whose key
    is opened, and a deactivated owner's is not opened at all.
    """

    owner = document.user
    if not owner.is_active:
        raise ForbiddenError("The document owner's account is not active.")
    record = UserSearchKey.objects.filter(user=owner, is_active=True).first()
    if record is None:
        raise InvalidRequestError("No active search key exists for this user.")
    return unwrap_search_key(record, master_key=master_key)


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
