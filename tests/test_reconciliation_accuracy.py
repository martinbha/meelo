"""Accuracy baselines for the sanitized reconciliation scenario corpus."""

from decimal import Decimal
from typing import Any

import pytest

from apps.reconciliation.accuracy import Accuracy, MatchKey, accuracy_by_match_type
from apps.reconciliation.models import ReconciliationMatch
from tests.factories import make_user
from tests.test_reconciliation_fixtures import SCENARIOS, build_world, detect

pytestmark = pytest.mark.django_db

BASELINE = {
    ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT: (Decimal(1), Decimal(0), Decimal(0)),
    ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION: (Decimal(1), Decimal(0), Decimal(0)),
    ReconciliationMatch.MatchType.INTERNAL_TRANSFER: (Decimal(1), Decimal(0), Decimal(0)),
    ReconciliationMatch.MatchType.REFUND_MATCH: (Decimal(1), Decimal(0), Decimal(0)),
}


def key(match_type: str, left: str, right: str) -> MatchKey:
    return MatchKey.normalized(match_type, left, right)


def test_accuracy_reports_true_missed_and_false_rates_per_match_type() -> None:
    expected = [
        key("duplicate_observation", "card", "bank"),
        key("internal_transfer", "checking", "savings"),
    ]
    actual = [
        key("duplicate_observation", "bank", "card"),
        key("duplicate_observation", "card", "unrelated"),
    ]

    metrics = accuracy_by_match_type(expected, actual)

    assert metrics["duplicate_observation"] == Accuracy(1, 0, 1)
    assert metrics["duplicate_observation"].true_match_rate == Decimal(1)
    assert metrics["duplicate_observation"].false_match_rate == Decimal("0.5")
    assert metrics["internal_transfer"] == Accuracy(0, 1, 0)
    assert metrics["internal_transfer"].missed_match_rate == Decimal(1)


def test_reconciliation_scenario_corpus_meets_the_per_type_baseline() -> None:
    """A scoring regression must worsen an explicit per-type baseline."""

    expected: list[MatchKey] = []
    actual: list[MatchKey] = []
    for index, scenario in enumerate(SCENARIOS):
        user = make_user(email=f"accuracy-{index}@example.com")
        world: dict[str, Any] = build_world(scenario, user)
        row_keys = {row.pk: key for key, row in world["rows"].items()}
        expected.extend(
            key(candidate.match_type, f"{index}:{candidate.left}", f"{index}:{candidate.right}")
            for candidate in scenario.expected_candidates
        )
        detect(scenario, world, user)
        actual.extend(
            key(
                match.match_type,
                f"{index}:{row_keys[match.left_observation_id]}",
                f"{index}:{row_keys[match.right_observation_id]}",
            )
            for match in ReconciliationMatch.objects.filter(user=user)
        )

    metrics = accuracy_by_match_type(expected, actual)

    assert set(metrics) == set(BASELINE)
    for match_type, baseline in BASELINE.items():
        measured = metrics[match_type]
        assert (
            measured.true_match_rate,
            measured.missed_match_rate,
            measured.false_match_rate,
        ) == baseline
