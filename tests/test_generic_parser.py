from decimal import Decimal

from apps.ocr.contracts import BoundingBox
from apps.parsing.contracts import DocumentMetadata, NormalizedToken, TransactionDirection
from apps.parsing.generic import GenericTransactionListParser


def token(text: str, left: int, top: int, *, sequence: int) -> NormalizedToken:
    return NormalizedToken(text, 0.9, BoundingBox(left, top, left + 50, top + 12), sequence)


def test_generic_parser_separates_multiple_coordinate_rows() -> None:
    tokens = (
        token("2026-08-15", 0, 10, sequence=0),
        token("cafe", 70, 11, sequence=1),
        token("debit", 150, 10, sequence=2),
        token("4200 KRW", 220, 12, sequence=3),
        token("2026-08-16", 0, 50, sequence=4),
        token("salary", 70, 51, sequence=5),
        token("credit", 150, 50, sequence=6),
        token("3000000 KRW", 220, 49, sequence=7),
    )

    observations = GenericTransactionListParser().parse(
        DocumentMetadata("unknown", 1080, 1920), tokens
    )

    assert len(observations) == 2
    assert observations[0].merchant == "cafe"
    assert observations[0].amount == Decimal("4200")
    assert observations[0].direction == TransactionDirection.DEBIT
    assert observations[1].merchant == "salary"
    assert observations[1].direction == TransactionDirection.CREDIT
    assert all(not item.missing_fields for item in observations)
    assert observations[0].source_region == BoundingBox(0, 10, 270, 24)


def test_generic_parser_never_invents_partial_values() -> None:
    observations = GenericTransactionListParser().parse(
        DocumentMetadata("unknown", None, None),
        (token("unknown merchant", 0, 10, sequence=0),),
    )

    assert len(observations) == 1
    assert observations[0].merchant == "unknown merchant"
    assert observations[0].occurred_on is None
    assert observations[0].amount is None
    assert observations[0].direction is None
    assert observations[0].missing_fields == frozenset({"date", "amount", "direction"})
    assert observations[0].confidence_factors["requires_review"] is True


def test_generic_parser_marks_ambiguous_amounts_for_review() -> None:
    observation = GenericTransactionListParser().parse(
        DocumentMetadata("unknown", None, None),
        (
            token("cafe", 0, 10, sequence=0),
            token("4200 KRW", 60, 10, sequence=1),
            token("4300 KRW", 120, 10, sequence=2),
        ),
    )[0]

    assert observation.amount is None
    assert observation.currency is None
    assert observation.ambiguous_fields == frozenset({"amount"})
    assert observation.confidence_factors["requires_review"] is True


def test_generic_parser_support_is_a_low_priority_fallback() -> None:
    parser = GenericTransactionListParser()
    support = parser.supports(
        DocumentMetadata("unknown", None, None),
        (
            token("2026-08-15", 0, 0, sequence=0),
            token("4200 KRW", 50, 0, sequence=1),
        ),
    )

    assert parser.metadata.name == "generic"
    assert 0 < support.score < 0.5
    assert support.detected_source_type == "generic_transaction_list"
