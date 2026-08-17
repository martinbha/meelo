"""Blind indexes for exact search over encrypted values (#93, spec 22.4).

An encrypted column cannot be queried, and a plain digest of a low-entropy value
is not an index — it is a lookup table waiting to be built. These tests hold the
tokens to being keyed, scoped, domain-separated, and versioned.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from typing import Any

import pytest

from apps.categorization.normalization import merchant_blind_index
from apps.core.blind_index import (
    BLIND_INDEX_VERSION,
    DOMAINS,
    MINIMUM_KEY_BYTES,
    BlindIndexError,
    blind_index,
    index_version,
    is_current,
)
from apps.core.key_management import derive_blind_index_key
from apps.observations.models import ImportedObservation
from apps.reconciliation.duplicates import (
    ObservationFacts,
    deterministic_key,
    find_duplicate_candidates,
    group_by_key,
)

KEY = os.urandom(32)
OTHER_KEY = os.urandom(32)


def facts(**overrides: Any) -> ObservationFacts:
    values: dict[str, Any] = {
        "observation_id": "row-1",
        "user_id": 1,
        "occurred_at": date(2026, 8, 15),
        "amount_minor": 4_200,
        "currency": "KRW",
        "direction": ImportedObservation.Direction.DEBIT,
        "merchant": "스타벅스",
        "approval_code": "300142",
        "balance_after_minor": None,
        "instrument_id": "card-1",
        "account_id": None,
        "source_type": "card_transaction_list",
        "source_document_id": "doc-1",
    }
    values.update(overrides)
    return ObservationFacts(**values)


# ---------------------------------------------------------------------------
# A plain digest is not an index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["300142", "4200", "스타벅스", "2026-08-15"])
def test_a_low_entropy_value_is_not_findable_by_plain_sha256(value: str) -> None:
    """Six digits is a million guesses. A keyed token makes that worthless."""

    token = blind_index("approval_code", value, user_id=1, key=KEY)
    plain = hashlib.sha256(value.encode()).hexdigest()

    assert plain not in token
    # And no amount of guessing the surrounding format helps without the key.
    for guess in (value, f"approval_code|1|{value}", f"1|approval_code|1|{value}"):
        assert hashlib.sha256(guess.encode()).hexdigest() not in token


def test_the_key_is_what_makes_the_token_unpredictable() -> None:
    first = blind_index("merchant", "스타벅스", user_id=1, key=KEY)
    second = blind_index("merchant", "스타벅스", user_id=1, key=OTHER_KEY)

    assert first != second


def test_a_key_shorter_than_the_digest_is_refused() -> None:
    with pytest.raises(BlindIndexError):
        blind_index("merchant", "스타벅스", user_id=1, key=b"x" * (MINIMUM_KEY_BYTES - 1))


# ---------------------------------------------------------------------------
# Scoping and separation
# ---------------------------------------------------------------------------


def test_two_users_never_share_a_token_for_one_value() -> None:
    """The database must not reveal that two people shop at the same café."""

    assert blind_index("merchant", "스타벅스", user_id=1, key=KEY) != blind_index(
        "merchant", "스타벅스", user_id=2, key=KEY
    )


def test_two_domains_never_share_a_token_for_one_value() -> None:
    """A merchant named "4200" must not match an approval code of 4200."""

    tokens = {blind_index(domain, "4200", user_id=1, key=KEY) for domain in sorted(DOMAINS)}

    assert len(tokens) == len(DOMAINS)


def test_an_unknown_domain_is_refused() -> None:
    """A typo would create a second index that matches nothing and says nothing."""

    with pytest.raises(BlindIndexError):
        blind_index("mercahnt", "스타벅스", user_id=1, key=KEY)


def test_an_empty_value_has_no_token() -> None:
    with pytest.raises(BlindIndexError):
        blind_index("merchant", "", user_id=1, key=KEY)


def test_the_same_value_always_gives_the_same_token() -> None:
    """Exact matching depends on it: a random token would match nothing."""

    tokens = {blind_index("merchant", "스타벅스", user_id=1, key=KEY) for _ in range(10)}

    assert len(tokens) == 1


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def test_a_token_says_which_scheme_produced_it() -> None:
    token = blind_index("merchant", "스타벅스", user_id=1, key=KEY)

    assert token.startswith(f"{BLIND_INDEX_VERSION}:")
    assert index_version(token) == BLIND_INDEX_VERSION
    assert is_current(token)


def test_a_token_from_another_version_is_a_different_token() -> None:
    """Otherwise a reindex would leave rows that look updated and are not."""

    current = blind_index("merchant", "스타벅스", user_id=1, key=KEY)
    older = blind_index("merchant", "스타벅스", user_id=1, key=KEY, version=1)
    future = blind_index("merchant", "스타벅스", user_id=1, key=KEY, version=2)

    assert current == older
    assert future != current
    assert index_version(future) == 2
    assert not is_current(future)


def test_a_token_with_no_version_is_reported_as_needing_a_rebuild() -> None:
    """Written before the version existed. Saying so beats raising mid-migration."""

    assert index_version("abc123") == 0
    assert index_version("notanumber:abc123") == 0
    assert not is_current("abc123")


def test_a_version_starts_at_one() -> None:
    with pytest.raises(BlindIndexError):
        blind_index("merchant", "스타벅스", user_id=1, key=KEY, version=0)


# ---------------------------------------------------------------------------
# The key comes from the user's own key
# ---------------------------------------------------------------------------


def test_the_search_key_is_derived_not_the_data_key() -> None:
    data_key = os.urandom(32)
    search_key = derive_blind_index_key(data_key)

    assert search_key != data_key
    assert blind_index("merchant", "스타벅스", user_id=1, key=search_key) != blind_index(
        "merchant", "스타벅스", user_id=1, key=data_key
    )


# ---------------------------------------------------------------------------
# Exact matching still works, without decrypting anything
# ---------------------------------------------------------------------------


def test_a_merchant_index_matches_across_spellings() -> None:
    """The point of the index: three spellings, one token, no decryption."""

    first = merchant_blind_index("스타벅스 강남점", user_id=1, key=KEY)
    second = merchant_blind_index("스타벅스강남", user_id=1, key=KEY)

    assert first == second
    assert is_current(first)


def test_duplicate_grouping_is_keyed_when_a_search_key_is_supplied() -> None:
    """Everything in this key is low entropy: a date, an amount, a six-digit code."""

    row = facts()
    keyed = deterministic_key(row, search_key=KEY)
    unkeyed = deterministic_key(row)

    assert keyed != unkeyed
    assert is_current(keyed)
    assert hashlib.sha256(b"approval|1|300142").hexdigest() == unkeyed


def test_keyed_grouping_still_finds_the_same_pairs() -> None:
    """Keying must not cost the matching it exists to protect."""

    left = facts(observation_id="left")
    right = facts(observation_id="right")

    grouped = group_by_key([left, right], search_key=KEY)

    assert len(grouped) == 1
    assert len(next(iter(grouped.values()))) == 2


def test_keyed_grouping_keeps_unrelated_rows_apart() -> None:
    left = facts(observation_id="left", approval_code="300142")
    right = facts(observation_id="right", approval_code="999999")

    assert group_by_key([left, right], search_key=KEY) == {}


def test_two_users_rows_never_group_together_under_one_key() -> None:
    mine = facts(observation_id="mine", user_id=1)
    theirs = facts(observation_id="theirs", user_id=2)

    assert group_by_key([mine, theirs], search_key=KEY) == {}


def test_duplicate_candidates_are_found_with_a_search_key() -> None:
    left = facts(observation_id="left")
    right = facts(observation_id="right")

    candidates = find_duplicate_candidates([left, right], search_key=KEY)

    assert len(candidates) == 1
    assert "same_approval_code" in candidates[0].features
