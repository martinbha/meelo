"""Turning confirmed history into a file the user can keep.

An export is the one point where financial data leaves the encrypted store in
readable form, which makes three things matter more here than anywhere else.

**Amounts stay in minor units.** Every figure is an integer number of the
currency's smallest unit, exactly as stored. A CSV that wrote 42900 as ``429.00``
would be lying about a currency with no minor unit, and a spreadsheet that opened
it would round it again for good measure. The currency travels beside the number
so a reader can divide if they want to.

**The field list is documented and fixed.** An export whose columns move between
versions cannot be diffed against last month's, which is most of what people
export for.

**A plaintext file is temporary.** A CSV of somebody's finances on disk is the
largest unencrypted surface this system creates. It exists so a person can save
it somewhere else, and it is deleted on a timer whether or not they did.

The encrypted format is the exception: sealed with a key derived from a
passphrase the user chose, it is the only form safe to keep once the timer has
run out.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from apps.core.crypto import read_model_field
from apps.core.errors import InvalidRequestError
from apps.transactions.classification import bucket_of
from apps.transactions.models import CanonicalTransaction

from .amounts import transaction_amount

#: The columns an export carries, in order. Fixed so this month's file can be
#: diffed against last month's, which is most of why people export.
EXPORT_FIELDS: tuple[str, ...] = (
    "transaction_id",
    "occurred_at",
    "posted_at",
    "transaction_type",
    "reporting_bucket",
    "amount_minor",
    "currency",
    "merchant",
    "counterparty",
    "category",
    "financial_account",
    "payment_instrument",
    "status",
    "category_source",
    "notes",
)

#: Header written above the ciphertext so a future reader knows what they have.
ARCHIVE_FORMAT = "meelo-export-v1"
_SALT_SIZE = 16
_NONCE_SIZE = 12
#: Argon2id parameters for the archive key. Deliberately expensive: the archive
#: may sit in cloud storage for years, and the only thing between it and a reader
#: is the strength of a passphrase a person typed once.
_TIME_COST = 3
_MEMORY_COST = 64 * 1024
_PARALLELISM = 4
MINIMUM_PASSPHRASE_LENGTH = 12


class ExportError(InvalidRequestError):
    """An export cannot be produced or read."""


def export_root() -> Path:
    """The private directory exports live in, created 0700 if missing."""

    raw = Path(
        getattr(settings, "EXPORT_TMP_ROOT", None) or Path(settings.DOCUMENT_TMP_ROOT) / "exports"
    )
    if raw.is_symlink():
        raise ExportError("The export root cannot be a symlink.")
    root = raw.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def safe_export_path(name: str) -> Path:
    """Resolve one export file inside the root, refusing anything outside it."""

    root = export_root()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ExportError("The export path is outside the export directory.")
    return candidate


def _label(instance: Any, field: str, *, data_key: bytes | None) -> str:
    if instance is None:
        return ""
    return read_model_field(instance, field, key=data_key)


def export_row(
    transaction: CanonicalTransaction, *, data_key: bytes | None = None
) -> dict[str, Any]:
    """One transaction as the documented fields.

    The amount is an integer in minor units and the currency is beside it. No
    division happens here: a reader who wants major units can do it knowing the
    currency, and this file will not have rounded first.
    """

    amount = transaction_amount(transaction, data_key=data_key)
    return {
        "transaction_id": str(transaction.pk),
        "occurred_at": transaction.occurred_at.isoformat(),
        "posted_at": transaction.posted_at.isoformat() if transaction.posted_at else "",
        "transaction_type": transaction.transaction_type,
        "reporting_bucket": bucket_of(transaction.transaction_type),
        "amount_minor": amount.amount_minor,
        "currency": amount.resolved_currency.code,
        "merchant": read_model_field(transaction, "merchant_encrypted", key=data_key),
        "counterparty": read_model_field(transaction, "counterparty_encrypted", key=data_key),
        "category": _label(transaction.category, "name_encrypted", data_key=data_key),
        "financial_account": _label(
            transaction.financial_account, "name_encrypted", data_key=data_key
        ),
        "payment_instrument": _label(
            transaction.payment_instrument, "name_encrypted", data_key=data_key
        ),
        "status": transaction.status,
        "category_source": transaction.category_source,
        "notes": read_model_field(transaction, "notes_encrypted", key=data_key),
    }


def export_rows(
    transactions: Iterable[CanonicalTransaction], *, data_key: bytes | None = None
) -> Iterator[dict[str, Any]]:
    """Stream the documented fields for a set of transactions."""

    for transaction in transactions:
        yield export_row(transaction, data_key=data_key)


def write_csv(rows: Iterable[Mapping[str, Any]], destination: Any) -> int:
    """Write rows as CSV with the documented header. Returns the row count."""

    writer = csv.DictWriter(destination, fieldnames=list(EXPORT_FIELDS), extrasaction="raise")
    writer.writeheader()
    written = 0
    for row in rows:
        writer.writerow(row)
        written += 1
    return written


def json_document(
    rows: Sequence[Mapping[str, Any]],
    *,
    period_start: date | None,
    period_end: date | None,
) -> dict[str, Any]:
    """The JSON envelope: what this is, then the rows.

    The header exists so a file found in three years explains itself — which
    format, which fields, and what period it covers.
    """

    return {
        "format": ARCHIVE_FORMAT,
        "fields": list(EXPORT_FIELDS),
        "amounts": "integer minor units, currency given per row",
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "row_count": len(rows),
        "transactions": [dict(row) for row in rows],
    }


def write_json(
    rows: Sequence[Mapping[str, Any]],
    destination: Any,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> int:
    """Write rows as one JSON document. Returns the row count."""

    json.dump(
        json_document(rows, period_start=period_start, period_end=period_end),
        destination,
        ensure_ascii=False,
        indent=2,
    )
    return len(rows)


def derive_archive_key(passphrase: str, salt: bytes) -> bytes:
    """Stretch a passphrase into an archive key with Argon2id."""

    if len(passphrase) < MINIMUM_PASSPHRASE_LENGTH:
        raise ExportError(
            f"An export passphrase must be at least {MINIMUM_PASSPHRASE_LENGTH} characters."
        )
    return hash_secret_raw(
        secret=passphrase.encode(),
        salt=salt,
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST,
        parallelism=_PARALLELISM,
        hash_len=32,
        type=Argon2Type.ID,
    )


def seal_archive(payload: bytes, *, passphrase: str) -> bytes:
    """Encrypt an export so it is safe to keep.

    The salt and nonce travel with the ciphertext, and the format header is
    authenticated as associated data — so a file whose header was edited to claim
    a different format fails to open rather than being read the wrong way.
    """

    salt = os.urandom(_SALT_SIZE)
    nonce = os.urandom(_NONCE_SIZE)
    key = derive_archive_key(passphrase, salt)
    sealed = AESGCM(key).encrypt(nonce, payload, ARCHIVE_FORMAT.encode())
    return b"".join((ARCHIVE_FORMAT.encode(), b"\n", salt, nonce, sealed))


def open_archive(blob: bytes, *, passphrase: str) -> bytes:
    """Decrypt an archive, refusing anything that has been altered."""

    header = ARCHIVE_FORMAT.encode() + b"\n"
    if not blob.startswith(header):
        raise ExportError("This file is not a Meelo export archive.")
    body = blob[len(header) :]
    if len(body) <= _SALT_SIZE + _NONCE_SIZE:
        raise ExportError("The export archive is truncated.")
    salt = body[:_SALT_SIZE]
    nonce = body[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
    sealed = body[_SALT_SIZE + _NONCE_SIZE :]
    key = derive_archive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, sealed, ARCHIVE_FORMAT.encode())
    except Exception as exc:  # noqa: BLE001 - any failure means the same thing
        raise ExportError("The passphrase is wrong or the archive has been altered.") from exc
