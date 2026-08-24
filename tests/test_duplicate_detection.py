import os
from datetime import date
from typing import Any

import pytest

from apps.observations.models import ImportedObservation
from apps.reconciliation.duplicates import (
    AUTOMATIC_MERGE_ENABLED,
    LIKELY_MERGE_SCORE,
    REVIEW_CANDIDATE_SCORE,
    ObservationFacts,
    deterministic_key,
    find_duplicate_candidates,
    group_by_key,
    score_pair,
)

DEBIT = ImportedObservation.Direction.DEBIT
CREDIT = ImportedObservation.Direction.CREDIT


#: Duplicate grouping is keyed; every low-entropy value in it needs one.
SEARCH_KEY = os.urandom(32)


def facts(**overrides: Any) -> ObservationFacts:
    values: dict[str, Any] = {
        "observation_id": "row-1",
        "user_id": 1,
        "occurred_at": date(2026, 8, 15),
        "amount_minor": 4200,
        "currency": "KRW",
        "direction": DEBIT,
        "merchant": "스타벅스 강남점",
        "approval_code": "",
        "balance_after_minor": None,
        "instrument_id": "card-1",
        "account_id": "account-1",
        "source_type": "card_transaction_list",
        "source_document_id": "doc-1",
    }
    values.update(overrides)
    return ObservationFacts(**values)


# ---------------------------------------------------------------------------
# Deterministic keys (#69)
# ---------------------------------------------------------------------------


def test_an_identical_approval_code_produces_one_key() -> None:
    left = facts(approval_code="12345678")
    right = facts(
        observation_id="row-2",
        approval_code="12345678",
        merchant="완전히 다른 이름",
        amount_minor=9999,
        source_document_id="doc-2",
    )

    assert deterministic_key(left, search_key=SEARCH_KEY) == deterministic_key(
        right, search_key=SEARCH_KEY
    )


def test_the_row_key_uses_instrument_date_amount_and_direction() -> None:
    left = facts()
    same = facts(observation_id="row-2", merchant="다른 이름", source_document_id="doc-2")
    different_amount = facts(observation_id="row-3", amount_minor=4300)

    assert deterministic_key(left, search_key=SEARCH_KEY) == deterministic_key(
        same, search_key=SEARCH_KEY
    )
    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
        different_amount, search_key=SEARCH_KEY
    )


def test_keys_never_collide_across_users() -> None:
    assert deterministic_key(facts(), search_key=SEARCH_KEY) != deterministic_key(
        facts(user_id=2), search_key=SEARCH_KEY
    )
    assert deterministic_key(
        facts(approval_code="1234"), search_key=SEARCH_KEY
    ) != deterministic_key(facts(user_id=2, approval_code="1234"), search_key=SEARCH_KEY)


def test_a_row_without_an_amount_or_date_has_no_key() -> None:
    assert deterministic_key(facts(amount_minor=None), search_key=SEARCH_KEY) == ""
    assert deterministic_key(facts(occurred_at=None), search_key=SEARCH_KEY) == ""
    # An approval code still identifies the row even without the rest.
    assert (
        deterministic_key(facts(amount_minor=None, approval_code="99"), search_key=SEARCH_KEY) != ""
    )


def test_grouping_returns_only_keys_with_more_than_one_row() -> None:
    grouped = group_by_key(
        [
            facts(),
            facts(observation_id="row-2", source_document_id="doc-2"),
            facts(observation_id="row-3", amount_minor=9999),
        ],
        search_key=SEARCH_KEY,
    )

    assert len(grouped) == 1
    assert {item.observation_id for item in next(iter(grouped.values()))} == {"row-1", "row-2"}


# ---------------------------------------------------------------------------
# Scoring (specification 16.3, #71)
# ---------------------------------------------------------------------------


def test_a_full_match_scores_a_proposed_merge() -> None:
    left = facts(approval_code="12345678", balance_after_minor=957_100)
    right = facts(
        observation_id="row-2",
        approval_code="12345678",
        balance_after_minor=957_100,
        source_document_id="doc-2",
    )

    score = score_pair(left, right)

    assert score.score >= LIKELY_MERGE_SCORE
    assert score.is_likely_merge is True
    assert score.strength == "likely_merge"
    assert "same_approval_code" in score.features
    assert "exact_amount" in score.features


def test_a_partial_match_is_a_review_candidate() -> None:
    # Same amount, same card, one day apart, same direction: 30+25+15+10 = 80.
    left = facts(merchant="", source_type="")
    right = facts(
        observation_id="row-2",
        occurred_at=date(2026, 8, 16),
        merchant="",
        source_type="",
        source_document_id="doc-2",
    )

    score = score_pair(left, right)

    assert REVIEW_CANDIDATE_SCORE <= score.score < LIKELY_MERGE_SCORE
    assert score.is_review_candidate is True
    assert score.strength == "review_candidate"


def test_unrelated_rows_stay_separate() -> None:
    left = facts()
    right = facts(
        observation_id="row-2",
        amount_minor=99_000,
        occurred_at=date(2026, 3, 2),
        merchant="전혀 다른 가게",
        instrument_id="card-9",
        account_id="account-9",
        source_type="bank_transaction_list",
        source_document_id="doc-2",
    )

    score = score_pair(left, right)

    assert score.keeps_separate is True
    assert score.strength == "weak"


def test_same_user_is_required_not_merely_weighted() -> None:
    score = score_pair(facts(), facts(observation_id="row-2", user_id=2))

    assert score.score == 0
    assert score.blocking_reason == "different users"


def test_an_unknown_direction_earns_no_direction_points() -> None:
    left = facts(direction=ImportedObservation.Direction.UNKNOWN)
    right = facts(observation_id="row-2", direction=ImportedObservation.Direction.UNKNOWN)

    assert "same_direction" not in score_pair(left, right).features


def test_similar_merchants_score_even_when_not_identical() -> None:
    left = facts(merchant="스타벅스 강남점")
    right = facts(observation_id="row-2", merchant="스타벅스 강남점 ")

    assert "merchant_similarity" in score_pair(left, right).features


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def test_duplicates_across_overlapping_screenshots_stay_traceable() -> None:
    left = facts(source_document_id="doc-1")
    right = facts(observation_id="row-2", source_document_id="doc-2")

    candidates = find_duplicate_candidates([left, right], search_key=SEARCH_KEY)

    assert len(candidates) == 1
    candidate = candidates[0]
    # Both sides keep their own document, so the evidence survives the pairing.
    assert candidate.left.source_document_id == "doc-1"
    assert candidate.right.source_document_id == "doc-2"


def test_a_deterministic_pair_is_always_returned_whatever_it_scores() -> None:
    # The approval code matches but nothing else does, so the weighted score
    # alone would fall under the review threshold.
    left = facts(approval_code="12345678")
    right = facts(
        observation_id="row-2",
        approval_code="12345678",
        amount_minor=99_999,
        occurred_at=date(2025, 1, 1),
        merchant="다른 가게",
        instrument_id="card-9",
        account_id="account-9",
        source_type="bank_transaction_list",
    )

    candidates = find_duplicate_candidates([left, right], search_key=SEARCH_KEY)

    assert len(candidates) == 1
    assert candidates[0].from_deterministic_key is True


def test_weak_pairs_are_not_proposed() -> None:
    left = facts()
    right = facts(
        observation_id="row-2",
        amount_minor=1,
        occurred_at=date(2020, 1, 1),
        merchant="관계없음",
        instrument_id=None,
        account_id=None,
        source_type="",
    )

    assert find_duplicate_candidates([left, right], search_key=SEARCH_KEY) == ()


def test_candidates_are_ordered_by_score() -> None:
    base = facts()
    strong = facts(observation_id="row-2", approval_code="")
    weaker = facts(
        observation_id="row-3",
        occurred_at=date(2026, 8, 16),
        merchant="",
        source_type="",
    )

    candidates = find_duplicate_candidates([base, strong, weaker], search_key=SEARCH_KEY)
    scores = [item.score.score for item in candidates]

    assert scores == sorted(scores, reverse=True)


def test_candidate_search_skips_non_deterministic_pairs_outside_the_window() -> None:
    left = facts(occurred_at=date(2026, 1, 1), approval_code="")
    right = facts(observation_id="row-2", occurred_at=date(2026, 3, 1), approval_code="")

    assert find_duplicate_candidates([left, right], search_key=SEARCH_KEY) == ()


def test_candidate_cap_keeps_the_best_pairs_and_emits_a_safe_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [facts(observation_id=f"row-{index}", approval_code="") for index in range(4)]
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "apps.reconciliation.duplicates.metrics.record",
        lambda name, **labels: emitted.append((name, labels)),
    )

    candidates = find_duplicate_candidates(
        rows,
        search_key=SEARCH_KEY,
        max_candidates_per_observation=1,
    )

    assert len(candidates) == 2
    assert all(
        sum(
            row.observation_id in (item.left.observation_id, item.right.observation_id)
            for item in candidates
        )
        <= 1
        for row in rows
    )
    assert emitted == [
        (
            "reconciliation.proposed",
            {"status": "capped", "match_type": "duplicate_observation"},
        )
    ]


def test_automatic_merging_is_disabled_for_the_initial_release() -> None:
    assert AUTOMATIC_MERGE_ENABLED is False
