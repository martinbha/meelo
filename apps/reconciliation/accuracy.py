"""Privacy-safe accuracy measurements for reconciliation candidates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple


class MatchKey(NamedTuple):
    """A sanitized fixture relationship, independent of database identifiers."""

    match_type: str
    left: str
    right: str

    @classmethod
    def normalized(cls, match_type: str, left: str, right: str) -> MatchKey:
        first, second = sorted((left, right))
        return cls(match_type, first, second)


@dataclass(frozen=True, slots=True)
class Accuracy:
    true_matches: int
    missed_matches: int
    false_matches: int

    @property
    def true_match_rate(self) -> Decimal:
        expected = self.true_matches + self.missed_matches
        return Decimal(self.true_matches) / expected if expected else Decimal(1)

    @property
    def missed_match_rate(self) -> Decimal:
        return Decimal(1) - self.true_match_rate

    @property
    def false_match_rate(self) -> Decimal:
        proposed = self.true_matches + self.false_matches
        return Decimal(self.false_matches) / proposed if proposed else Decimal(0)


def accuracy_by_match_type(
    expected: Iterable[MatchKey], actual: Iterable[MatchKey]
) -> dict[str, Accuracy]:
    """Compare fixture truth with proposals and report each relationship kind."""

    expected_set = set(expected)
    actual_set = set(actual)
    match_types = {key.match_type for key in expected_set | actual_set}
    return {
        match_type: Accuracy(
            true_matches=len(
                {key for key in expected_set & actual_set if key.match_type == match_type}
            ),
            missed_matches=len(
                {key for key in expected_set - actual_set if key.match_type == match_type}
            ),
            false_matches=len(
                {key for key in actual_set - expected_set if key.match_type == match_type}
            ),
        )
        for match_type in sorted(match_types)
    }
