"""Searchable tokens for encrypted values.

An encrypted column cannot be queried. A blind index is what makes exact
matching possible anyway: a keyed digest of the *normalized* value, stored beside
the ciphertext, that the database can compare without ever holding the value.

The word doing the work is **keyed**. A plain digest of a low-entropy value is
not an index, it is a lookup table waiting to be built. There are only so many
amounts a coffee costs, only so many dates in a year, and a six-digit approval
code has a million possibilities — an attacker holding the database can hash all
of them in seconds and read the column straight off. HMAC with a key they do not
have removes that entirely: without the key there is nothing to compare against
(specification 22.4).

Three properties follow, and each is tested:

- **Domain separation.** A merchant named ``4200`` and an amount of ``4200``
  must not produce the same token, or a match in one column would imply a match
  in another.
- **Per-user scoping.** Two people who shop at the same café get different
  tokens, so the database cannot reveal that they have anything in common.
- **A visible key version.** Every token says which search key produced it, so
  a reindex can find the old ones without holding either key, and a query
  during a rotation can look for both at once (#168).

The key itself is derived from the user's data key and never stored — see
:func:`apps.core.key_management.derive_blind_index_key`. It is a secret of the
same standing as the encryption key, and anything derived from it is treated the
same way.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .errors import InvalidRequestError

#: Bumped when the token *construction* changes — a different digest, a
#: different payload layout. It lives inside the HMAC rather than in the
#: prefix, because it is not what a reindex selects on.
BLIND_INDEX_SCHEME = 1

#: What the prefix carries: which **search key** built the token. That is the
#: thing that changes on a rotation, and the thing a query has to be able to
#: recognise without holding either key. A scheme version in the prefix would
#: have been useless for the operation the prefix exists to support — rotating
#: the key produces a different digest under an identical prefix, so a partly
#: reindexed table would be indistinguishable from a fully reindexed one.
DEFAULT_KEY_VERSION = 1

#: Minimum key length. Shorter than the digest gains nothing and hides the
#: mistake of passing something that is not a key at all.
MINIMUM_KEY_BYTES = 32

#: Domains a token can be built for. Named rather than free-form: a typo in a
#: domain string would silently create a second, incompatible index that matches
#: nothing and reports no error.
DOMAINS: frozenset[str] = frozenset(
    {
        "merchant",
        "counterparty",
        "institution",
        "approval_code",
        "identifier",
        "observation_row",
    }
)


class BlindIndexError(InvalidRequestError):
    """A blind index cannot be built from these inputs."""


def blind_index(
    domain: str,
    value: str,
    *,
    user_id: Any,
    key: bytes,
    version: int = DEFAULT_KEY_VERSION,
) -> str:
    """A searchable token for one value, revealing nothing about it.

    Returns ``"<version>:<hex digest>"``. The version is outside the digest on
    purpose: it has to be readable without the key so a reindex can find old
    tokens, and it is not a secret.
    """

    if domain not in DOMAINS:
        raise BlindIndexError(f"Unknown blind-index domain: {domain!r}.")
    if len(key) < MINIMUM_KEY_BYTES:
        raise BlindIndexError(f"Blind-index keys must contain at least {MINIMUM_KEY_BYTES} bytes.")
    if version < 1:
        raise BlindIndexError("Blind-index key versions start at one.")
    if not value:
        raise BlindIndexError("An empty value has no blind index.")

    # The scheme, domain, key version, and owner all go inside the digest, so a
    # token cannot be replayed against another column, another scheme, another
    # key version, or another person. The key version is repeated outside it
    # because a query has to read it without holding the key.
    payload = f"{BLIND_INDEX_SCHEME}|{version}|{domain}|{user_id}|{value}".encode()
    return f"{version}:{hmac.new(key, payload, hashlib.sha256).hexdigest()}"


def index_version(token: str) -> int:
    """Which search key version produced this token.

    Returns 0 for anything that carries no version — a token written before the
    prefix existed, or an empty column. Those need rebuilding, and saying so is
    more useful than raising in the middle of a reindex over a million rows.
    """

    prefix, separator, _ = token.partition(":")
    if not separator:
        return 0
    try:
        return int(prefix)
    except ValueError:
        return 0


def is_current(token: str, *, key_version: int = DEFAULT_KEY_VERSION) -> bool:
    """Whether this token was built by the search key currently in force."""

    return index_version(token) == key_version
