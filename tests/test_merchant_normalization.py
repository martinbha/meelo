"""Merchant normalization and alias matching (#82, specification 6.7, 6.11, 18).

The same shop arrives under several names. A card app prints
``(주)스타벅스코리아 강남점``, the bank prints ``스타벅스강남``, and OCR turns one
of them into ``스타벅스 강남 점``. They are one merchant, and a rule the user
wrote once has to fire on all three — without the system ever showing them a
name they have not seen on a receipt.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from apps.categorization.models import Category, MerchantAlias
from apps.categorization.normalization import (
    REVIEW_THRESHOLD,
    SIMILARITY_THRESHOLD,
    best_candidate,
    display_merchant,
    merchant_blind_index,
    normalize_merchant,
    rank_candidates,
    similarity,
)
from apps.categorization.services import (
    categorize_transaction,
    create_exact_merchant_rule,
    create_merchant_alias,
    find_merchant_alias,
    suggest_merchant_aliases,
)
from apps.core.errors import InvalidRequestError
from apps.observations.review import accept_observation, decrypt_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

ENCRYPTION_KEY = os.urandom(32)
BLIND_INDEX_KEY = os.urandom(32)


@pytest.fixture
def owner() -> Any:
    return make_user(email="normalization-owner@example.com")


def make_category(user: Any, name: str) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"norm-{name}",
        category_type=Category.CategoryType.EXPENSE,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("스타벅스 강남점", "스타벅스강남"),
        ("스타벅스 강남 점", "스타벅스강남점"),
        ("(주)스타벅스코리아", "주식회사 스타벅스코리아"),
        ("CORNER SHOP", "corner  shop"),
        ("Corner-Shop!", "Corner Shop"),
        ("체크카드 이마트", "이마트"),
        ("이마트 승인 1234567", "이마트"),
        ("ＣＡＦＥ", "cafe"),
    ],
)
def test_equivalent_spellings_reduce_to_one_key(left: str, right: str) -> None:
    assert normalize_merchant(left) == normalize_merchant(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("스타벅스", "투썸플레이스"),
        ("이마트", "홈플러스"),
        ("corner shop", "corner store"),
    ],
)
def test_different_merchants_keep_different_keys(left: str, right: str) -> None:
    assert normalize_merchant(left) != normalize_merchant(right)


def test_a_name_that_normalizes_to_nothing_is_refused() -> None:
    """An empty key would collide with every other empty key."""

    with pytest.raises(InvalidRequestError):
        normalize_merchant("   ")
    with pytest.raises(InvalidRequestError):
        normalize_merchant("!!!")


def test_the_display_value_keeps_the_name_the_source_printed() -> None:
    """A normalized name is a lookup key, not a label."""

    printed = "(주)스타벅스코리아  강남점"

    assert display_merchant(printed) == "(주)스타벅스코리아 강남점"
    assert display_merchant(printed) != normalize_merchant(printed)


def test_normalization_is_stable_across_repeated_calls() -> None:
    value = "(주)스타벅스코리아 강남점"

    assert len({normalize_merchant(value) for _ in range(5)}) == 1


# ---------------------------------------------------------------------------
# Blind indexes
# ---------------------------------------------------------------------------


def test_two_spellings_share_a_blind_index(owner: Any) -> None:
    first = merchant_blind_index("스타벅스 강남점", user_id=owner.pk, key=BLIND_INDEX_KEY)
    second = merchant_blind_index("스타벅스강남", user_id=owner.pk, key=BLIND_INDEX_KEY)

    assert first == second


def test_two_users_never_share_an_index_for_one_merchant(owner: Any) -> None:
    """An attacker holding the database must not learn who shops where."""

    stranger = make_user(email="normalization-stranger@example.com")

    assert merchant_blind_index(
        "스타벅스", user_id=owner.pk, key=BLIND_INDEX_KEY
    ) != merchant_blind_index("스타벅스", user_id=stranger.pk, key=BLIND_INDEX_KEY)


def test_the_index_reveals_nothing_about_the_name(owner: Any) -> None:
    index = merchant_blind_index("스타벅스 강남점", user_id=owner.pk, key=BLIND_INDEX_KEY)

    assert "스타벅스" not in index
    assert len(index) == 64
    # And a different key gives a different answer, so a plain digest of the
    # name cannot be used to confirm a guess.
    assert index != merchant_blind_index("스타벅스 강남점", user_id=owner.pk, key=os.urandom(32))


def test_a_short_key_is_refused(owner: Any) -> None:
    with pytest.raises(InvalidRequestError):
        merchant_blind_index("스타벅스", user_id=owner.pk, key=b"short")


# ---------------------------------------------------------------------------
# Fuzzy matching, in memory only
# ---------------------------------------------------------------------------


def test_a_close_spelling_scores_highly() -> None:
    assert similarity("스타벅스 강남점", "스타벅스 강남역점") >= SIMILARITY_THRESHOLD


def test_an_unrelated_merchant_scores_low() -> None:
    assert similarity("스타벅스", "홈플러스") < REVIEW_THRESHOLD


def test_an_unusable_name_scores_nothing_rather_than_raising() -> None:
    assert similarity("스타벅스", "   ") == 0.0


def test_candidates_come_back_strongest_first() -> None:
    ranked = rank_candidates(
        "corner shop seoul", ["corner shop seoul", "corner shop seou", "corner shop"]
    )

    assert [item.candidate for item in ranked][0] == "corner shop seoul"
    assert [item.score for item in ranked] == sorted((item.score for item in ranked), reverse=True)


def test_uncertain_matches_are_returned_rather_than_hidden() -> None:
    """A weak suggestion a reviewer can see is one they can correct."""

    ranked = rank_candidates("corner shop seoul", ["corner shop seou"])

    assert ranked
    assert ranked[0].needs_review or ranked[0].is_confident


def test_matches_below_the_review_threshold_are_dropped() -> None:
    assert rank_candidates("스타벅스", ["홈플러스", "이마트"]) == ()
    assert best_candidate("스타벅스", ["홈플러스"]) is None


# ---------------------------------------------------------------------------
# Alias lookup end to end
# ---------------------------------------------------------------------------


def test_an_alias_written_once_is_found_by_another_spelling(owner: Any) -> None:
    create_merchant_alias(
        user=owner,
        alias="스타벅스 강남점",
        normalized_merchant="스타벅스",
        default_category=make_category(owner, "coffee"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )

    found = find_merchant_alias(
        user=owner, merchant="스타벅스강남", blind_index_key=BLIND_INDEX_KEY
    )

    assert found is not None


def test_suggestions_surface_an_alias_no_exact_lookup_would_find(owner: Any) -> None:
    create_merchant_alias(
        user=owner,
        alias="corner shop seoul",
        normalized_merchant="corner shop seoul",
        default_category=make_category(owner, "groceries"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )

    assert (
        find_merchant_alias(
            user=owner, merchant="corner shop seou", blind_index_key=BLIND_INDEX_KEY
        )
        is None
    )
    suggestions = suggest_merchant_aliases(
        user=owner, merchant="corner shop seou", encryption_key=ENCRYPTION_KEY
    )

    assert suggestions
    assert suggestions[0].score >= REVIEW_THRESHOLD


def test_suggestions_never_reach_another_users_aliases(owner: Any) -> None:
    stranger = make_user(email="normalization-thief@example.com")
    create_merchant_alias(
        user=stranger,
        alias="corner shop seoul",
        normalized_merchant="corner shop seoul",
        default_category=make_category(stranger, "theirs"),
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )

    assert (
        suggest_merchant_aliases(
            user=owner, merchant="corner shop seoul", encryption_key=ENCRYPTION_KEY
        )
        == ()
    )
    assert MerchantAlias.objects.filter(user=stranger).exists()


# ---------------------------------------------------------------------------
# The whole chain: two spellings, one rule
# ---------------------------------------------------------------------------


def parsed(merchant: str) -> ParsedObservation:
    return ParsedObservation(
        occurred_on=date(2026, 8, 15),
        amount=Decimal("4200"),
        currency="KRW",
        direction=TransactionDirection.DEBIT,
        merchant=merchant,
        confidence_factors={"token_confidence": 0.95, "amount_confidence": 0.95},
        parser_name="toss_bank",
        parser_version="1.0",
        parser_support_score=0.95,
    )


def test_two_spellings_of_one_shop_share_a_category_rule(owner: Any) -> None:
    account = make_account(owner, name_blind_index="norm-account")
    coffee = make_category(owner, "coffee")
    create_exact_merchant_rule(
        user=owner,
        merchant="스타벅스 강남점",
        category=coffee,
        encryption_key=ENCRYPTION_KEY,
        blind_index_key=BLIND_INDEX_KEY,
        key_version=1,
    )
    document = make_document(owner, file_sha256="8" * 64)
    run = make_ocr_run(owner, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            # Three spellings of one shop: no space, a stray space before
            # the branch marker, and the card app's payment prefix.
            (
                parsed("스타벅스강남"),
                parsed("스타벅스 강남 점"),
                parsed("체크카드 스타벅스 강남점"),
            ),
        ),
        data_key=ENCRYPTION_KEY,
        key_version=1,
        blind_index_key=BLIND_INDEX_KEY,
    ).observations

    for row in rows:
        transaction = accept_observation(
            row.pk,
            user=owner,
            data_key=ENCRYPTION_KEY,
            financial_account=account,
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        )
        decision = categorize_transaction(transaction_id=transaction.pk, user=owner)
        assert decision.category == coffee


def test_the_raw_merchant_survives_import_unchanged(owner: Any) -> None:
    """The user reads what the source printed, not what the index needed."""

    printed = "(주)스타벅스코리아 강남점"
    document = make_document(owner, file_sha256="9" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(printed),),
        ),
        data_key=ENCRYPTION_KEY,
        key_version=1,
        blind_index_key=BLIND_INDEX_KEY,
    ).observations[0]

    values = decrypt_observation(row, user=owner, data_key=ENCRYPTION_KEY)

    assert values.merchant == printed
    assert row.merchant_normalized_encrypted
    assert row.merchant_blind_index


def test_import_without_a_search_key_stores_no_index(owner: Any) -> None:
    """The raw text is still kept; only the lookup is skipped."""

    document = make_document(owner, file_sha256="a" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed("스타벅스"),),
        ),
        data_key=ENCRYPTION_KEY,
        key_version=1,
    ).observations[0]

    assert row.merchant_blind_index == ""
    assert row.merchant_raw_encrypted
