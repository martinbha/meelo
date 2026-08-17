"""Reading a transaction's amount for arithmetic.

Amounts are stored as ``minor_units:CURRENCY`` — integer minor units, never a
float and never a major-unit decimal, because a report that adds up money has to
end on the same number a person would reach with a pen. Rounding enters this
system exactly once, when a parser reads a screen, and never again.

Some rows carry that string encrypted and some carry it in clear, because field
encryption reached this model after the first rows were written (#163). The
envelope is recognised by its version prefix rather than by attempting a decrypt
and catching the failure: a genuine decryption failure has to stay loud, and a
report that silently skipped rows it could not read would produce a total that
is wrong in the one direction nobody checks.
"""

from __future__ import annotations

from typing import Any

from apps.core.crypto import decrypt_model_field, is_encrypted_value
from apps.core.value_objects import Money
from apps.ledger.posting import deserialize_money


def is_encrypted(value: str) -> bool:
    """Whether a stored amount is a ciphertext envelope rather than plaintext."""

    return is_encrypted_value(value)


def transaction_amount(transaction: Any, *, data_key: bytes | None = None) -> Money:
    """The amount on one transaction, decrypting only when it has to.

    Raises rather than guessing when a row is encrypted and no key was supplied:
    a missing key is a caller mistake, and answering zero would quietly shrink
    a month.
    """

    stored = transaction.amount_encrypted
    if not is_encrypted(stored):
        return deserialize_money(stored)
    if data_key is None:
        raise ValueError("This transaction's amount is encrypted and no data key was supplied.")
    return deserialize_money(decrypt_model_field(transaction, "amount_encrypted", key=data_key))
