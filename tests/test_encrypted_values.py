"""Structured values that survive a round trip, or refuse to (#164, specification 15.1, 22.3, 22.5).

An envelope holds a string, so everything else has to become one first. The
danger is not the encoding — it is the decoding, where ``json.loads`` returns
whatever the payload happened to be and hands a string to a caller expecting a
mapping. The failure then surfaces several frames away, somewhere with no idea
which column it came from.

So each read states the shape it expects, and a payload that is not that shape
is refused where it is read.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from apps.core.crypto import InvalidCiphertextError
from apps.core.encrypted_values import (
    MalformedPayloadError,
    decode_json,
    decode_money,
    encode_json,
    encode_money,
    money_from_minor,
)
from apps.core.value_objects import Money
from tests.factories import make_account, make_transaction, make_user

KEY = os.urandom(32)

NESTED = {
    "engine": "paddleocr",
    "languages": ["ko", "en"],
    "options": {"device": "cpu", "threads": 4},
    "variants": [{"scale": 2.0, "name": "sharpen"}, {"scale": 1.0, "name": "plain"}],
    "succeeded": True,
    "attempts": 3,
}


# ----------------------------------------------------------------------
# Round trips
# ----------------------------------------------------------------------


def test_a_nested_structure_keeps_its_sequence_order_and_value_types() -> None:
    restored = decode_json(encode_json(NESTED), expected=dict)

    assert restored == NESTED
    # Lists are data: their order is part of the value.
    assert restored["languages"] == ["ko", "en"]
    assert [item["name"] for item in restored["variants"]] == ["sharpen", "plain"]
    # And a bool does not come back as an int, nor an int as a float.
    assert restored["succeeded"] is True
    assert isinstance(restored["attempts"], int)
    assert isinstance(restored["variants"][0]["scale"], float)


def test_mapping_keys_come_back_sorted_because_the_encoding_is_canonical() -> None:
    """Two equal values must encode identically, or nothing can compare them."""

    one = encode_json({"b": 1, "a": 2})
    other = encode_json({"a": 2, "b": 1})

    assert one == other == '{"a":2,"b":1}'
    assert list(decode_json(one, expected=dict)) == ["a", "b"]


def test_unicode_survives_rather_than_being_escaped() -> None:
    """The escaped form triples the ciphertext for Korean text."""

    payload = encode_json({"merchant": "스타벅스"})

    assert "스타벅스" in payload
    assert decode_json(payload, expected=dict)["merchant"] == "스타벅스"


# ----------------------------------------------------------------------
# Failing closed
# ----------------------------------------------------------------------


@pytest.mark.django_db
def test_a_truncated_payload_fails_authentication_rather_than_decoding_partially() -> None:
    """The cipher refuses it; the decoder never sees it."""

    user = make_user(email="values-owner@example.com")
    transaction = make_transaction(user, make_account(user))
    transaction.encrypt_json_field("notes_encrypted", NESTED, key=KEY)
    sealed = transaction.notes_encrypted

    transaction.notes_encrypted = sealed[:-8]
    with pytest.raises(InvalidCiphertextError):
        transaction.read_json_field("notes_encrypted", key=KEY, expected=dict)

    # And an altered byte inside the ciphertext, not merely a short one.
    body = list(sealed)
    body[-1] = "A" if body[-1] != "A" else "B"
    transaction.notes_encrypted = "".join(body)
    with pytest.raises(InvalidCiphertextError):
        transaction.read_json_field("notes_encrypted", key=KEY, expected=dict)


def test_a_payload_of_the_wrong_shape_is_refused_by_name() -> None:
    with pytest.raises(MalformedPayloadError, match="str, not dict"):
        decode_json('"a bare string"', expected=dict)
    with pytest.raises(MalformedPayloadError, match="not list"):
        decode_json("{}", expected=list)


def test_a_payload_that_is_not_json_is_refused() -> None:
    with pytest.raises(MalformedPayloadError):
        decode_json("{not json", expected=dict)
    with pytest.raises(MalformedPayloadError):
        decode_json("", expected=dict)


def test_a_value_that_cannot_be_encoded_is_refused_at_the_write() -> None:
    with pytest.raises(MalformedPayloadError):
        encode_json({"when": object()})


@pytest.mark.django_db
def test_an_empty_column_reads_as_the_default_rather_than_raising() -> None:
    """A match stored without features has no features, which is an answer."""

    user = make_user(email="values-empty@example.com")
    transaction = make_transaction(user, make_account(user))

    assert transaction.read_json_field("notes_encrypted", key=KEY, default=[]) == []


# ----------------------------------------------------------------------
# Money
# ----------------------------------------------------------------------


def test_an_amount_round_trips_with_its_currency_inside_the_payload() -> None:
    """The currency travels inside, so the associated data covers it too."""

    assert encode_money(Money(42_900, "KRW")) == "42900:KRW"
    assert decode_money("42900:KRW") == Money(42_900, "KRW")
    assert decode_money(encode_money(Money(-1_025, "USD"))) == Money(-1_025, "USD")


def test_a_float_amount_is_refused_rather_than_rounded() -> None:
    """0.1 + 0.2 is not 0.3, and a total of near-misses is wrong by nothing anyone can name."""

    with pytest.raises(MalformedPayloadError, match="never a float"):
        money_from_minor(42.9, "KRW")
    with pytest.raises(MalformedPayloadError):
        money_from_minor(Decimal("42.9"), "KRW")
    with pytest.raises(MalformedPayloadError):
        money_from_minor("42900", "KRW")


def test_a_boolean_is_not_an_amount() -> None:
    """Python says True is an int. Money(True, "KRW") is not what anybody meant."""

    with pytest.raises(MalformedPayloadError):
        money_from_minor(True, "KRW")


def test_a_whole_number_of_minor_units_is_accepted() -> None:
    assert money_from_minor(42_900, "KRW") == Money(42_900, "KRW")
    assert money_from_minor(0, "USD") == Money(0, "USD")


def test_a_malformed_amount_is_refused_rather_than_read_as_zero() -> None:
    for payload in ("", "not-money", "42900", "abc:KRW", "42900:ZZZ"):
        with pytest.raises(MalformedPayloadError):
            decode_money(payload)


def test_only_a_money_value_can_be_encoded_as_an_amount() -> None:
    with pytest.raises(MalformedPayloadError):
        encode_money(42_900)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Through the model
# ----------------------------------------------------------------------


@pytest.mark.django_db
def test_the_mixin_round_trips_a_structure_under_the_row_identity() -> None:
    user = make_user(email="values-mixin@example.com")
    transaction = make_transaction(user, make_account(user))

    transaction.encrypt_json_field("notes_encrypted", NESTED, key=KEY)

    assert transaction.notes_encrypted != encode_json(NESTED)
    assert transaction.read_json_field("notes_encrypted", key=KEY, expected=dict) == NESTED


@pytest.mark.django_db
def test_the_mixin_round_trips_an_amount() -> None:
    user = make_user(email="values-money@example.com")
    transaction = make_transaction(user, make_account(user))

    transaction.encrypt_money_field("amount_encrypted", Money(42_900, "KRW"), key=KEY)

    assert transaction.read_money_field("amount_encrypted", key=KEY) == Money(42_900, "KRW")
    assert "42900" not in transaction.amount_encrypted


@pytest.mark.django_db
def test_a_structure_moved_to_another_row_does_not_open() -> None:
    user = make_user(email="values-bound@example.com")
    account = make_account(user)
    first = make_transaction(user, account)
    second = make_transaction(user, account)

    first.encrypt_json_field("notes_encrypted", NESTED, key=KEY)
    second.notes_encrypted = first.notes_encrypted

    with pytest.raises(InvalidCiphertextError):
        second.read_json_field("notes_encrypted", key=KEY, expected=dict)


@pytest.mark.django_db
def test_reconciliation_features_survive_the_switch_to_the_helper() -> None:
    """The helper replaced hand-written serialization; the behaviour is unchanged."""

    from apps.reconciliation.models import ReconciliationMatch
    from apps.reconciliation.services import _encrypted_features, decrypt_match_features

    user = make_user(email="values-features@example.com")
    match = ReconciliationMatch(
        user=user,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        match_score=95,
    )
    features = ("same_amount", "same_approval_code", "same_day")

    _encrypted_features(match, features, data_key=KEY, key_version=1)

    assert "same_amount" not in match.match_features_json_encrypted
    assert decrypt_match_features(match, data_key=KEY) == features


@pytest.mark.django_db
def test_unreadable_features_show_no_reasons_rather_than_breaking_the_queue() -> None:
    """A candidate's score still stands even when its evidence cannot be read."""

    from apps.reconciliation.models import ReconciliationMatch
    from apps.reconciliation.services import decrypt_match_features

    user = make_user(email="values-broken@example.com")
    match = ReconciliationMatch(
        user=user,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        match_score=95,
    )
    # Authenticates, because it was sealed properly — but it is a mapping where
    # the reader expects a list of names.
    match.encrypt_json_field("match_features_json_encrypted", {"a": 1}, key=KEY)

    assert decrypt_match_features(match, data_key=KEY) == ()
