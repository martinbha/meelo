"""Accuracy baselines for the sanitized reconciliation scenario corpus."""

from decimal import Decimal

from apps.reconciliation.accuracy import Accuracy, MatchKey, accuracy_by_match_type
from apps.reconciliation.models import ReconciliationMatch

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


def test_reconciliation_accuracy_baseline_covers_every_implemented_match_type() -> None:
    """A scoring regression must worsen an explicit per-type baseline."""

    for match_type, (true_rate, missed_rate, false_rate) in BASELINE.items():
        assert match_type in ReconciliationMatch.MatchType.values
        assert true_rate == Decimal(1)
        assert missed_rate == Decimal(0)
        assert false_rate == Decimal(0)
