"""How risky a row is to accept without looking at it.

Risk is scored once, at import, and stored on the observation so the review
queue can order every open row in the database rather than only within a page.
The score is the single worst problem a row has, not a sum: three cosmetic
problems must never outrank one ambiguous amount.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: Confidence at or below which a row counts as low confidence.
LOW_CONFIDENCE_THRESHOLD = 0.7

#: Risk contributed by each parser flag.
FLAG_RISK: Mapping[str, int] = {
    "ambiguous_amount": 100,
    "missing_amount": 95,
    "balance_mismatch": 90,
    "parser_error": 85,
    "unknown_direction": 80,
    "missing_direction": 80,
    "missing_date": 70,
    "ambiguous_date": 70,
    "parser_fallback": 50,
    "settlement_candidate": 45,
    "ambiguous_merchant": 30,
    "missing_merchant": 20,
}

#: Risk contributed by an unmapped account or card, which blocks ledger posting.
UNKNOWN_MAPPING_RISK = 75
LOW_CONFIDENCE_RISK = 40

#: At or above this score a row is treated as high risk and sorted to the top.
HIGH_RISK_THRESHOLD = 80

#: Flags that mean the amount itself is in doubt.
AMOUNT_FLAGS = frozenset({"ambiguous_amount", "missing_amount"})
MISSING_FLAGS = frozenset(
    {"missing_amount", "missing_date", "missing_direction", "missing_merchant"}
)


def score_flags(
    flags: Sequence[str],
    *,
    overall_confidence: float,
    has_mapping: bool,
) -> tuple[int, tuple[str, ...]]:
    """Score a row and list its problems, worst first."""

    scored: list[tuple[int, str]] = []
    for flag in flags:
        risk = FLAG_RISK.get(str(flag))
        if risk is not None:
            scored.append((risk, str(flag)))
    if not has_mapping:
        scored.append((UNKNOWN_MAPPING_RISK, "unknown_mapping"))
    if overall_confidence <= LOW_CONFIDENCE_THRESHOLD:
        scored.append((LOW_CONFIDENCE_RISK, "low_confidence"))
    if not scored:
        return 0, ()
    highest = max(score for score, _ in scored)
    reasons = tuple(reason for _, reason in sorted(scored, key=lambda item: (-item[0], item[1])))
    return highest, reasons


def projections(flags: Sequence[str]) -> dict[str, bool]:
    """The queryable booleans derived from a row's flags."""

    names = {str(flag) for flag in flags}
    return {
        "amount_uncertain": bool(names & AMOUNT_FLAGS),
        "balance_mismatched": "balance_mismatch" in names,
        "has_missing_fields": bool(names & MISSING_FLAGS),
        "is_settlement_candidate": "settlement_candidate" in names,
    }
