from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class FinancialField(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    MERCHANT = "merchant"
    DIRECTION = "direction"
    INSTRUMENT = "instrument"
    BALANCE = "balance"


FIELD_WEIGHTS: Mapping[FinancialField, float] = MappingProxyType(
    {
        FinancialField.AMOUNT: 0.25,
        FinancialField.DATE: 0.15,
        FinancialField.MERCHANT: 0.15,
        FinancialField.DIRECTION: 0.15,
        FinancialField.INSTRUMENT: 0.15,
        FinancialField.BALANCE: 0.15,
    }
)
ENGINE_WEIGHT = 0.5
AGREEMENT_WEIGHT = 0.3
VALIDATION_WEIGHT = 0.2


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    engine_confidences: tuple[float, ...]
    engines_agree: bool
    parser_valid: bool
    value_present: bool = True
    known_instrument: bool = True

    def __post_init__(self) -> None:
        if any(not 0.0 <= value <= 1.0 for value in self.engine_confidences):
            raise ValueError("Engine confidence must be between zero and one.")


@dataclass(frozen=True, slots=True)
class FieldConfidence:
    field: FinancialField
    score: float
    requires_review: bool
    factors: Mapping[str, float | bool | str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "factors", MappingProxyType(dict(self.factors)))


@dataclass(frozen=True, slots=True)
class ObservationConfidence:
    combined_score: float
    requires_review: bool
    fields: Mapping[FinancialField, FieldConfidence]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


def score_field(
    field: FinancialField,
    evidence: FieldEvidence,
    *,
    review_threshold: float = 0.75,
) -> FieldConfidence:
    if not 0.0 <= review_threshold <= 1.0:
        raise ValueError("Review threshold must be between zero and one.")
    engine_score = (
        sum(evidence.engine_confidences) / len(evidence.engine_confidences)
        if evidence.engine_confidences
        else 0.0
    )
    agreement_score = 1.0 if evidence.engines_agree else 0.0
    validation_score = 1.0 if evidence.parser_valid else 0.0
    score = round(
        engine_score * ENGINE_WEIGHT
        + agreement_score * AGREEMENT_WEIGHT
        + validation_score * VALIDATION_WEIGHT,
        6,
    )
    if not evidence.value_present:
        score = 0.0
    hard_reason = ""
    if not evidence.value_present:
        hard_reason = "missing_value"
    elif field == FinancialField.AMOUNT and not evidence.engines_agree:
        hard_reason = "amount_disagreement"
    elif field == FinancialField.INSTRUMENT and not evidence.known_instrument:
        hard_reason = "unknown_instrument"
    requires_review = bool(hard_reason) or score < review_threshold
    return FieldConfidence(
        field=field,
        score=score,
        requires_review=requires_review,
        factors={
            "engine_score": round(engine_score, 6),
            "engine_count": len(evidence.engine_confidences),
            "engines_agree": evidence.engines_agree,
            "parser_valid": evidence.parser_valid,
            "value_present": evidence.value_present,
            "known_instrument": evidence.known_instrument,
            "hard_review_reason": hard_reason,
        },
    )


def score_observation(
    evidence: Mapping[FinancialField, FieldEvidence],
    *,
    review_threshold: float = 0.75,
) -> ObservationConfidence:
    fields = {
        field: score_field(field, evidence[field], review_threshold=review_threshold)
        for field in FinancialField
    }
    combined = round(sum(fields[field].score * FIELD_WEIGHTS[field] for field in FinancialField), 6)
    return ObservationConfidence(
        combined_score=combined,
        requires_review=any(result.requires_review for result in fields.values()),
        fields=fields,
    )
