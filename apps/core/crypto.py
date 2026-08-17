from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = "v1"
#: 96-bit nonces, drawn from ``os.urandom`` for every single encryption. Never a
#: counter: a counter has to be persisted, and a counter that is restored from a
#: backup repeats — which for GCM is not a degraded cipher but a broken one, since
#: two messages under one key and nonce leak their XOR and the authentication key.
#: Random 96-bit nonces instead carry a birthday bound: the chance of a collision
#: stays below 2^-32 up to roughly 2^32 encryptions *per key*. Keys here are
#: per-user and per-version, so that bound is four billion field writes for one
#: person before rotation — far past anything this system will see, and #94 rotates
#: long before it matters.
NONCE_SIZE = 12
TAG_SIZE = 16


class EncryptionError(ValueError):
    """Base error for invalid encryption configuration or ciphertext."""


class InvalidCiphertextError(EncryptionError):
    """Ciphertext is malformed, has the wrong context, or failed authentication."""


@dataclass(frozen=True, slots=True)
class FieldContext:
    model: str
    record_id: str
    field: str
    user_id: str

    def associated_data(self, *, key_version: int) -> bytes:
        return (
            f"{FORMAT_VERSION}|{key_version}|{self.model}|{self.record_id}|{self.field}|"
            f"{self.user_id}"
        ).encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode(), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidCiphertextError("Encrypted field encoding is invalid.") from exc


def _validate_key(key: bytes) -> None:
    if len(key) != 32:
        raise EncryptionError("AES-256-GCM keys must contain exactly 32 bytes.")


def encrypt_value(
    plaintext: str,
    *,
    key: bytes,
    context: FieldContext,
    key_version: int,
) -> str:
    """Encrypt text into a versioned nonce/ciphertext/tag envelope."""

    _validate_key(key)
    if key_version < 1:
        raise EncryptionError("Encryption key versions must be positive.")
    nonce = os.urandom(NONCE_SIZE)
    encrypted = AESGCM(key).encrypt(
        nonce, plaintext.encode(), context.associated_data(key_version=key_version)
    )
    ciphertext, tag = encrypted[:-TAG_SIZE], encrypted[-TAG_SIZE:]
    return ".".join(
        (FORMAT_VERSION, str(key_version), _encode(nonce), _encode(ciphertext), _encode(tag))
    )


def decrypt_value(envelope: str, *, key: bytes, context: FieldContext) -> str:
    """Authenticate and decrypt one versioned encrypted-field envelope."""

    _validate_key(key)
    try:
        format_version, raw_key_version, raw_nonce, raw_ciphertext, raw_tag = envelope.split(".")
        key_version = int(raw_key_version)
    except (TypeError, ValueError) as exc:
        raise InvalidCiphertextError("Encrypted field envelope is invalid.") from exc
    if format_version != FORMAT_VERSION or key_version < 1:
        raise InvalidCiphertextError("Encrypted field version is unsupported.")
    nonce = _decode(raw_nonce)
    ciphertext = _decode(raw_ciphertext)
    tag = _decode(raw_tag)
    if len(nonce) != NONCE_SIZE or len(tag) != TAG_SIZE:
        raise InvalidCiphertextError("Encrypted field envelope is invalid.")
    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext + tag,
            context.associated_data(key_version=key_version),
        )
        return plaintext.decode()
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise InvalidCiphertextError("Encrypted field authentication failed.") from exc


def model_field_context(instance: Any, field: str, *, user_id: Any = None) -> FieldContext:
    """The associated data binding one value to one field of one row.

    ``user_id`` may be supplied for records that hold no owner of their own —
    a ledger entry belongs to whoever owns its transaction. Passing it keeps the
    ciphertext bound to a person without inventing a column, and the reader has
    to pass the same one, so a value cannot be opened under the wrong owner.
    """

    record_id = getattr(instance, "pk", None)
    owner = user_id if user_id is not None else getattr(instance, "user_id", None)
    if record_id is None or owner is None:
        raise EncryptionError("Encrypted model fields require record and user identifiers.")
    return FieldContext(
        model=f"{instance._meta.app_label}.{instance._meta.model_name}",
        record_id=str(record_id),
        field=field,
        user_id=str(owner),
    )


def encrypt_model_field(
    instance: Any,
    field: str,
    plaintext: str,
    *,
    key: bytes,
    key_version: int,
) -> str:
    return encrypt_value(
        plaintext,
        key=key,
        context=model_field_context(instance, field),
        key_version=key_version,
    )


def encrypt_model_fields(
    instance: Any,
    values: Mapping[str, str],
    *,
    key: bytes,
    key_version: int,
    user_id: Any = None,
) -> None:
    """Encrypt several fields onto an instance in place.

    Called after the instance has an identity, because the associated data binds
    each value to its record: a ciphertext moved to another row, or another
    field, or another user, fails to open rather than decrypting into the wrong
    place. An empty value is left alone — encrypting "" would store a ciphertext
    where the absence of a value is the value.
    """

    for field, plaintext in values.items():
        if not plaintext:
            continue
        setattr(
            instance,
            field,
            encrypt_value(
                plaintext,
                key=key,
                context=model_field_context(instance, field, user_id=user_id),
                key_version=key_version,
            ),
        )


def decrypt_model_field(instance: Any, field: str, *, key: bytes, user_id: Any = None) -> str:
    return decrypt_value(
        getattr(instance, field),
        key=key,
        context=model_field_context(instance, field, user_id=user_id),
    )


def is_encrypted_value(value: str) -> bool:
    """Whether a stored field holds a ciphertext envelope rather than plaintext.

    Field encryption reached some models after their first rows were written, so
    both forms exist side by side until those rows are re-encrypted (#163). The
    envelope is recognised by its version prefix rather than by attempting a
    decrypt and catching the failure: a genuine authentication failure has to
    stay loud, and swallowing it would let a caller treat ciphertext as a value.
    """

    return value.startswith(f"{FORMAT_VERSION}.")


def read_model_field(
    instance: Any, field: str, *, key: bytes | None = None, user_id: Any = None
) -> str:
    """Read a field that may or may not be encrypted yet.

    Raises when the field is encrypted and no key was supplied, rather than
    returning the envelope. A caller that got ciphertext back would go on to
    display it, index it, or add it up.
    """

    value = getattr(instance, field) or ""
    if not value or not is_encrypted_value(value):
        return value
    if key is None:
        raise EncryptionError(f"Field {field!r} is encrypted and no key was supplied.")
    return decrypt_model_field(instance, field, key=key, user_id=user_id)
