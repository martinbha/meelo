"""Reading and writing a transaction's money, encrypted or not.

An amount is stored as ``minor_units:CURRENCY``, and that string is what gets
encrypted. Keeping the encoding inside the ciphertext rather than beside it means
the currency cannot be edited in the database to make an amount mean something
else — the associated data covers the whole envelope, so a tampered row fails to
open instead of reporting a different figure.

Both forms exist in the wild: rows written before field encryption reached this
model hold the encoding in clear (#163). One module owns the distinction so no
caller has to know about it, and so the two cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from apps.core.crypto import encrypt_model_field, read_model_field
from apps.core.value_objects import Currency, Money
from apps.ledger.posting import deserialize_money, serialize_money


def read_money(instance: Any, field: str, *, data_key: bytes | None = None) -> Money:
    """The amount on one field, decrypting only if it has to.

    Raises when the field is encrypted and no key was supplied, rather than
    answering zero: a silently skipped amount is a total that is wrong in the one
    direction nobody checks.
    """

    return deserialize_money(read_model_field(instance, field, key=data_key))


def store_money(
    instance: Any,
    field: str,
    amount: Money,
    *,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> str:
    """Encode an amount onto a field, encrypting it when a key is available.

    Without a key the encoding is stored in clear. That path exists for tests and
    for the fixtures that predate encryption; every production caller supplies a
    key, and ``tests/test_field_encryption.py`` holds the web paths to it.
    """

    encoded = serialize_money(amount)
    setattr(instance, field, encoded)
    if data_key is None:
        return encoded
    ciphertext = encrypt_model_field(
        instance, field, encoded, key=data_key, key_version=key_version
    )
    setattr(instance, field, ciphertext)
    return ciphertext


def money_from(amount_minor: int, currency: str) -> Money:
    """Build a Money from integer minor units and a currency code."""

    return Money(int(amount_minor), Currency(currency))
