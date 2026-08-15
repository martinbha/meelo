from apps.ocr.confidence import (
    FieldEvidence,
    FinancialField,
    score_field,
    score_observation,
)


def strong_evidence(**overrides: object) -> FieldEvidence:
    values = {
        "engine_confidences": (0.9, 0.8),
        "engines_agree": True,
        "parser_valid": True,
        "value_present": True,
        "known_instrument": True,
    }
    values.update(overrides)
    return FieldEvidence(**values)  # type: ignore[arg-type]


def test_amount_disagreement_always_requires_review() -> None:
    result = score_field(
        FinancialField.AMOUNT,
        strong_evidence(engine_confidences=(1.0, 1.0), engines_agree=False),
    )

    assert result.score == 0.7
    assert result.requires_review is True
    assert result.factors["hard_review_reason"] == "amount_disagreement"


def test_unknown_instrument_always_requires_review() -> None:
    result = score_field(
        FinancialField.INSTRUMENT,
        strong_evidence(known_instrument=False),
    )

    assert result.score == 0.925
    assert result.requires_review is True
    assert result.factors["hard_review_reason"] == "unknown_instrument"


def test_combined_score_exposes_every_field_factor() -> None:
    evidence = {field: strong_evidence() for field in FinancialField}
    evidence[FinancialField.BALANCE] = strong_evidence(
        engine_confidences=(), parser_valid=False, value_present=False
    )

    result = score_observation(evidence)

    assert set(result.fields) == set(FinancialField)
    assert result.combined_score == 0.78625
    assert result.requires_review is True
    assert result.fields[FinancialField.BALANCE].factors["hard_review_reason"] == (
        "missing_value"
    )
