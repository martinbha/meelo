"""What each searchable value is normalized to before it is indexed.

A blind index only matches when both sides produce the same token, so the
normalization is as much part of the index as the key is. Getting it wrong does
not raise — it produces a token that matches nothing, and a lookup that quietly
returns no rows. That failure looks exactly like "there is nothing there", which
is why the rules live in one place and are shared by the write path and the
lookup path rather than being written out twice.

Each domain gets the treatment its values actually need:

- **Approval codes** are digits and letters printed on a receipt. Case and
  spacing vary between a card app and a bank app for the same authorisation, so
  both are removed. Separators are removed too: ``12-3456`` and ``123456`` are
  one code.
- **Account identifiers** arrive masked in half a dozen shapes — ``****1234``,
  ``1234-****-****-5678``, ``(1234)``. Only the digits survive, because the
  masking characters are the bank's presentation, not part of the number.
- **Institutions** and **counterparties** are names: case-folded, whitespace
  collapsed. They are not run through merchant normalization, which strips
  branch suffixes and payment-processor noise that a bank name legitimately
  contains.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .blind_index import blind_index

_SEPARATORS = re.compile(r"[\s\-_./\\()\[\]]+")
_NON_DIGITS = re.compile(r"\D+")
_WHITESPACE = re.compile(r"\s+")


def _folded(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def normalize_approval_code(value: str) -> str:
    """One authorisation's code, however the screen spelled it."""

    return _SEPARATORS.sub("", _folded(value))


def normalize_identifier(value: str) -> str:
    """The digits of an account or card number, masking removed.

    Returns ``""`` when nothing but masking was given: an index over ``****``
    would match every masked account the user has, which is worse than no index
    because it looks like a hit.
    """

    return _NON_DIGITS.sub("", unicodedata.normalize("NFKC", value))


def normalize_name(value: str) -> str:
    """An institution or counterparty name, folded and de-spaced."""

    return _WHITESPACE.sub(" ", _folded(value))


def approval_code_index(value: str, *, user_id: Any, key: bytes) -> str:
    normalized = normalize_approval_code(value)
    return blind_index("approval_code", normalized, user_id=user_id, key=key) if normalized else ""


def identifier_index(value: str, *, user_id: Any, key: bytes) -> str:
    normalized = normalize_identifier(value)
    return blind_index("identifier", normalized, user_id=user_id, key=key) if normalized else ""


def institution_index(value: str, *, user_id: Any, key: bytes) -> str:
    normalized = normalize_name(value)
    return blind_index("institution", normalized, user_id=user_id, key=key) if normalized else ""


def counterparty_index(value: str, *, user_id: Any, key: bytes) -> str:
    normalized = normalize_name(value)
    return blind_index("counterparty", normalized, user_id=user_id, key=key) if normalized else ""
