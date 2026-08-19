"""One door in and out of every encrypted column.

Before this, each service called :mod:`apps.core.crypto` for itself. That works
until it doesn't: the primitives take a key, a version, a field name, and
sometimes an owner identifier, and a caller who forgets the owner, passes the
wrong field name, or takes the ``data_key is None`` branch writes a value that
is either unopenable later or readable now. Neither failure is visible at the
point it happens — a plaintext merchant name in an ``_encrypted`` column looks
exactly like a working one until somebody reads the database.

So the write path is one method on a mixin, and it works out the associated data
from the instance rather than from its caller. A field bound to the wrong record
is no longer something a service can express.

Two properties this buys, both tested:

**Ciphertext is bound to its place.** Model, primary key, field name, and owner
all go into the AES-GCM associated data. A value moved to another row, another
column, or another user's record fails to open rather than decrypting into the
wrong place.

**Every encrypted column is declared.** ``encrypted_fields`` names them, and a
test walks the schema and fails on any ``*_encrypted`` column a model has not
declared. Adding a column and forgetting the encryption is the mistake this is
built to catch, because that column will hold real financial data on the first
write and nothing else will notice.

Records that hold no owner of their own — a ledger entry belongs to whoever owns
its transaction — override :meth:`EncryptedFieldsMixin.encryption_owner_id`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from django.conf import settings
from django.db import models

from .crypto import (
    EncryptionError,
    decrypt_model_field,
    encrypt_model_field,
    is_encrypted_value,
    read_model_field,
)


class UndeclaredEncryptedFieldError(EncryptionError):
    """A field was written through the shared path without being declared."""


class PlaintextWriteError(EncryptionError):
    """A sensitive value was about to be stored without a key to seal it."""


def encryption_required() -> bool:
    """Whether a missing key is an error rather than a plaintext write.

    True everywhere except the test settings. Several write paths accept
    ``data_key=None`` and store the value in clear — a convenience for tests and
    for fixtures that predate encryption, and a hole in production, where the
    column would then hold a readable merchant name that looks exactly like a
    working one.

    A setting rather than a hard requirement because closing the hole outright
    means every fixture in the suite has to carry a key, and a suite that is
    tedious to write is a suite that gets thinner. The compensation is
    ``tests/test_plaintext_encryption.py``, which turns the requirement on and
    drives the real services through it — so the production configuration is
    tested even though it is not the default the other tests run under.
    """

    return bool(getattr(settings, "FIELD_ENCRYPTION_REQUIRED", True))


def require_encryption_key(data_key: bytes | None, *, field: str) -> None:
    """Refuse to store a sensitive value in clear where that is not allowed."""

    if data_key is None and encryption_required():
        raise PlaintextWriteError(f"{field!r} is an encrypted column and no key was supplied.")


def encrypted_column_names(model: type[models.Model]) -> tuple[str, ...]:
    """Every ``*_encrypted`` column on a model, read from the schema.

    Derived rather than declared, so the completeness check has something
    independent to compare the declaration against. A list checked against
    itself checks nothing.
    """

    return tuple(
        field.attname
        for field in model._meta.concrete_fields
        if field.attname.endswith("_encrypted")
    )


class EncryptedFieldsMixin(models.Model):
    """Shared read and write path for a model's encrypted columns."""

    #: Every encrypted column on this model. Named explicitly so adding a column
    #: is a decision rather than an omission.
    encrypted_fields: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    @property
    def encryption_owner_id(self) -> Any:
        """Whose data this row is, for the associated data.

        Defaults to the row's own owner. Records that have none — a ledger entry
        belongs to whoever owns its transaction — override this, so a ciphertext
        is still bound to a person without inventing a column for it.
        """

        return getattr(self, "user_id", None)

    def _check_declared(self, fields: Iterable[str]) -> None:
        undeclared = sorted(set(fields) - set(self.encrypted_fields))
        if undeclared:
            raise UndeclaredEncryptedFieldError(
                f"{self._meta.label} does not declare {', '.join(undeclared)} as encrypted."
            )

    def encrypt_fields(
        self,
        values: Mapping[str, str],
        *,
        key: bytes,
        key_version: int = 1,
    ) -> None:
        """Encrypt several values onto this instance, in place.

        An empty value is assigned as-is rather than encrypted. A ciphertext
        where the absence of a value is the value would make "no note" and "a
        note nobody can read" indistinguishable, and every reader would have to
        decrypt to find out which it had.
        """

        self._check_declared(values)
        owner = self.encryption_owner_id
        for field, plaintext in values.items():
            if not plaintext:
                setattr(self, field, "")
                continue
            setattr(
                self,
                field,
                encrypt_model_field(
                    self,
                    field,
                    plaintext,
                    key=key,
                    key_version=key_version,
                    user_id=owner,
                ),
            )

    def decrypt_field(self, field: str, *, key: bytes) -> str:
        """Open one field, or fail. Never returns the envelope."""

        self._check_declared([field])
        return decrypt_model_field(self, field, key=key, user_id=self.encryption_owner_id)

    def read_field(self, field: str, *, key: bytes | None = None) -> str:
        """Read a field that may or may not be encrypted yet.

        Both forms exist while rows written before encryption reached a model
        are re-encrypted (#163). Raises when the value is an envelope and no key
        was given, rather than handing back ciphertext — a caller that got
        ciphertext would go on to display it, index it, or add it up.
        """

        self._check_declared([field])
        return read_model_field(self, field, key=key, user_id=self.encryption_owner_id)

    def plaintext_fields(self) -> tuple[str, ...]:
        """Declared fields currently holding something readable.

        Empty is not plaintext: a blank column holds no value to protect.
        """

        return tuple(
            field
            for field in self.encrypted_fields
            if (getattr(self, field, "") or "") and not is_encrypted_value(getattr(self, field))
        )

    @property
    def is_fully_encrypted(self) -> bool:
        return not self.plaintext_fields()
