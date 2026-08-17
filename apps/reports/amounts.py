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

from apps.core.crypto import is_encrypted_value
from apps.core.value_objects import Money
from apps.transactions.money import read_money


def is_encrypted(value: str) -> bool:
    """Whether a stored amount is a ciphertext envelope rather than plaintext."""

    return is_encrypted_value(value)


def transaction_amount(transaction: Any, *, data_key: bytes | None = None) -> Money:
    """The amount on one transaction, decrypting only when it has to.

    Delegates to :func:`apps.transactions.money.read_money`, which owns the rule.
    Two implementations of "how do I read an amount" is one too many for
    something every total depends on — and it raises rather than guessing when a
    row is encrypted and no key was supplied, because answering zero would
    quietly shrink a month.
    """

    return read_money(transaction, "amount_encrypted", data_key=data_key)
