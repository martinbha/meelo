from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = "v1"
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


def model_field_context(instance: Any, field: str) -> FieldContext:
    record_id = getattr(instance, "pk", None)
    user_id = getattr(instance, "user_id", None)
    if record_id is None or user_id is None:
        raise EncryptionError("Encrypted model fields require record and user identifiers.")
    return FieldContext(
        model=f"{instance._meta.app_label}.{instance._meta.model_name}",
        record_id=str(record_id),
        field=field,
        user_id=str(user_id),
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


def decrypt_model_field(instance: Any, field: str, *, key: bytes) -> str:
    return decrypt_value(
        getattr(instance, field),
        key=key,
        context=model_field_context(instance, field),
    )
