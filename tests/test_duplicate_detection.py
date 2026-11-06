import os
from datetime import date
from typing import Any

import pytest

from apps.observations.models import ImportedObservation
from apps.reconciliation.duplicates import (
    LIKELY_MERGE_SCORE,
    REVIEW_CANDIDATE_SCORE,
    ObservationFacts,
    automatic_merge_blocking_reason,
    deterministic_key,
    find_duplicate_candidates,
    group_by_key,
    safe_for_automatic_merge,
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


def test_approval_code_is_not_enough_to_produce_a_deterministic_key() -> None:
    left = facts(approval_code="12345678")
    right = facts(
        observation_id="row-2",
        approval_code="12345678",
        occurred_at=date(2026, 4, 1),
        source_document_id="doc-2",
    )

    assert (
        deterministic_key(facts(amount_minor=None, approval_code="12345678"), search_key=SEARCH_KEY)
        == ""
    )
    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
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
    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
        facts(observation_id="row-4", currency="USD"), search_key=SEARCH_KEY
    )
    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
        facts(observation_id="row-5", direction=CREDIT), search_key=SEARCH_KEY
    )
    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
        facts(observation_id="row-6", instrument_id="card-2"), search_key=SEARCH_KEY
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
    # An approval code supports a score but never substitutes for row identity.
    assert (
        deterministic_key(facts(amount_minor=None, approval_code="99"), search_key=SEARCH_KEY) == ""
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
    # Same mapped row identity is found even when a stricter score threshold
    # asks for stronger evidence than the usual review-candidate threshold.
    left = facts(merchant="", source_type="", approval_code="")
    right = facts(observation_id="row-2", merchant="", source_type="", approval_code="")

    candidates = find_duplicate_candidates([left, right], search_key=SEARCH_KEY, minimum_score=90)

    assert len(candidates) == 1
    assert candidates[0].from_deterministic_key is True
    assert candidates[0].score.score < 90


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


def test_exact_row_key_pair_wins_the_candidate_cap() -> None:
    base = facts(observation_id="base", approval_code="code-a", source_document_id="doc-base")
    exact = facts(observation_id="exact", approval_code="code-b", source_document_id="doc-exact")
    high_score = facts(
        observation_id="weighted",
        occurred_at=date(2026, 8, 16),
        approval_code="",
        source_document_id="doc-weighted",
    )

    candidates = find_duplicate_candidates(
        [base, high_score, exact],
        search_key=SEARCH_KEY,
        max_candidates_per_observation=1,
    )

    selected_ids = {
        candidates[0].left.observation_id,
        candidates[0].right.observation_id,
    }
    assert selected_ids == {"base", "exact"}
    assert candidates[0].from_deterministic_key is True


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"currency": "USD"}, "different currencies"),
        ({"amount_minor": 4300}, "different amounts"),
        ({"direction": CREDIT}, "different directions"),
        ({"instrument_id": "card-2"}, "different payment instruments"),
        ({"account_id": "account-2"}, "different financial accounts"),
    ],
)
def test_conflicting_facts_are_hard_blocked(changes: dict[str, Any], reason: str) -> None:
    left = facts(approval_code="reused-code")
    right = facts(observation_id="row-2", approval_code="reused-code", **changes)

    score = score_pair(left, right)
    candidates = find_duplicate_candidates([left, right], search_key=SEARCH_KEY)

    assert score.score == 0
    assert score.blocking_reason == reason
    assert candidates == ()


def test_reused_code_across_distant_dates_is_not_deterministic_or_proposed() -> None:
    left = facts(approval_code="ABC123", occurred_at=date(2026, 1, 1))
    right = facts(
        observation_id="row-2",
        approval_code="ABC123",
        occurred_at=date(2026, 4, 1),
        source_document_id="doc-2",
    )

    assert deterministic_key(left, search_key=SEARCH_KEY) != deterministic_key(
        right, search_key=SEARCH_KEY
    )
    assert find_duplicate_candidates([left, right], search_key=SEARCH_KEY) == ()


def test_reused_code_within_search_window_stays_reviewable_but_not_auto_merge_safe() -> None:
    left = facts(approval_code="ABC123", occurred_at=date(2026, 1, 1))
    right = facts(
        observation_id="row-2",
        approval_code="ABC123",
        occurred_at=date(2026, 1, 11),
        source_document_id="doc-2",
    )

    candidates = find_duplicate_candidates([left, right], search_key=SEARCH_KEY)

    assert len(candidates) == 1
    assert candidates[0].from_deterministic_key is False
    assert "same_approval_code" not in candidates[0].features
    assert candidates[0].score.score < LIKELY_MERGE_SCORE
    assert automatic_merge_blocking_reason(left, right) == "dates are more than one day apart"
    assert safe_for_automatic_merge(left, right) is False


def test_automatic_merge_requires_distinct_documents_and_matching_mapping() -> None:
    left = facts(approval_code="A123", source_document_id="doc-1")
    overlapping = facts(observation_id="row-2", approval_code="A123", source_document_id="doc-2")
    repeated_in_one_document = facts(
        observation_id="row-3", approval_code="A123", source_document_id="doc-1"
    )
    other_card = facts(
        observation_id="row-4",
        approval_code="A123",
        instrument_id="card-2",
        account_id="account-2",
        source_document_id="doc-3",
    )

    assert safe_for_automatic_merge(left, overlapping) is True
    assert safe_for_automatic_merge(left, repeated_in_one_document) is False
    assert safe_for_automatic_merge(left, other_card) is False
