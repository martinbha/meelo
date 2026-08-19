"""Encoding a structure or an amount so it can be encrypted, and read back safely.

An AES-GCM envelope holds a string. Anything that is not one — a match's feature
list, an OCR configuration, a region on a screenshot, an amount — has to be
turned into text first, and every service that did that for itself wrote the
same three lines slightly differently. Three problems came with that:

**Nothing checked what came back.** ``json.loads`` on a decrypted payload returns
whatever the payload happens to be. A caller expecting a mapping and handed a
string finds out several frames later, somewhere that has no idea what went
wrong. These helpers state the shape they expect and refuse anything else at the
point of reading.

**The encoding was not canonical.** Two equal values could produce different
text depending on dictionary insertion order, so the ciphertexts differed and
nothing could be compared or deduplicated. Mappings are key-sorted here, always.

**Money went through the same path as everything else.** An amount is not a
number, it is an integer count of minor units plus a currency, and a float
anywhere near it is a rounding error waiting for a total to be wrong
(specification 15.1). It gets its own encoding and its own refusal.

Failure is closed on purpose. A payload that has been truncated or altered fails
the cipher's authentication and never reaches the decoder — and one that
authenticates but decodes to the wrong shape raises rather than being coerced.
Half a value is not a value.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import InvalidRequestError
from .value_objects import Currency, InvalidMoneyError, Money


class MalformedPayloadError(InvalidRequestError):
    """A decrypted payload is not the shape its column promised."""


def encode_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace, Unicode kept.

    Sorted because two equal values must encode identically — otherwise the
    same match features written twice produce different ciphertexts and nothing
    downstream can compare them. Sequence order is preserved; a list is data,
    a mapping's key order is not.

    Unicode is kept rather than escaped. ``\\uc2a4`` and ``스`` are the same
    string, but only one of them is the same length as what a person typed, and
    the escaped form triples the ciphertext for Korean text.
    """

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise MalformedPayloadError(f"Value cannot be encoded as JSON: {exc}") from exc


def decode_json(payload: str, *, expected: type | tuple[type, ...] = object) -> Any:
    """Decode a payload and refuse anything that is not the expected shape.

    ``expected`` is not decoration. A caller that asked for a mapping and was
    handed a bare string would carry on until something tried to index it, by
    which point the failure has nothing to do with the column it came from.
    """

    if not payload:
        raise MalformedPayloadError("An empty payload has no decoded value.")
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise MalformedPayloadError("The decrypted payload is not valid JSON.") from exc
    if expected is not object and not isinstance(value, expected):
        names = (
            expected.__name__
            if isinstance(expected, type)
            else "/".join(item.__name__ for item in expected)
        )
        raise MalformedPayloadError(
            f"The decrypted payload is {type(value).__name__}, not {names}."
        )
    return value


def encode_money(amount: Money) -> str:
    """``minor_units:CURRENCY``.

    The currency travels *inside* the ciphertext rather than in a column beside
    it, so the associated data covers both. A currency edited in the database to
    make an amount mean something else fails to open instead of reporting a
    different figure.
    """

    if not isinstance(amount, Money):
        raise MalformedPayloadError("Only a Money value can be encoded as an amount.")
    return f"{amount.amount_minor}:{amount.resolved_currency.code}"


def decode_money(payload: str) -> Money:
    """Read an encoded amount back, or refuse."""

    try:
        minor, _, currency = payload.partition(":")
        return Money(int(minor), Currency(currency))
    except (TypeError, ValueError, InvalidMoneyError) as exc:
        raise MalformedPayloadError("Amounts must be encoded as minor_units:CURRENCY.") from exc


def money_from_minor(amount_minor: Any, currency: str) -> Money:
    """Build an amount from minor units, refusing anything that is not an integer.

    A float is refused rather than rounded. ``0.1 + 0.2`` is not ``0.3``, and a
    total built from values that were each nearly right is wrong by an amount
    nobody can account for — which is exactly the kind of error a personal
    finance system exists to not make (specification 15.1).

    ``bool`` is refused too: it is an ``int`` as far as Python is concerned, and
    ``Money(True, "KRW")`` is not an amount anybody meant to write.
    """

    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise MalformedPayloadError("Amounts must be a whole number of minor units, never a float.")
    return Money(amount_minor, Currency(currency))
