from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from apps.ocr.contracts import BoundingBox
from apps.parsing.contracts import (
    PARSER_OUTPUT_VERSION,
    DocumentMetadata,
    NormalizedToken,
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
    TransactionDirection,
)


class StubParser(ScreenshotParser):
    @property
    def metadata(self) -> ParserMetadata:
        return ParserMetadata("stub", "1.2.0")

    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        return ParserSupport(0.8, "transaction_list", ("transaction rows detected",))

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        return (
            ParsedObservation(
                occurred_on=date(2026, 8, 15),
                amount=Decimal("4200"),
                currency="KRW",
                direction=TransactionDirection.DEBIT,
                merchant="cafe",
                source_region=BoundingBox(0, 0, 100, 20),
                confidence_factors={"parser_valid": True},
            ),
            ParsedObservation(
                occurred_on=None,
                amount=None,
                currency=None,
                direction=None,
                merchant="unknown row",
                source_region=BoundingBox(0, 30, 100, 50),
                missing_fields=frozenset({"date", "amount", "direction"}),
                ambiguous_fields=frozenset({"merchant"}),
            ),
        )


def test_parser_contract_consumes_normalized_tokens_and_returns_multiple_rows() -> None:
    document = DocumentMetadata("unknown", 1080, 1920)
    tokens = (NormalizedToken("cafe", 0.9, BoundingBox(1, 1, 20, 10), 0, ("primary",)),)
    parser = StubParser()

    support = parser.supports(document, tokens)
    observations = parser.parse(document, tokens)

    assert support.score == 0.8
    assert parser.metadata == ParserMetadata("stub", "1.2.0")
    assert len(observations) == 2
    assert observations[0].amount == Decimal("4200")
    assert observations[0].amount_minor == 4200
    assert observations[0].output_version == PARSER_OUTPUT_VERSION


def test_parser_contract_keeps_missing_and_ambiguous_fields_explicit() -> None:
    observation = StubParser().parse(DocumentMetadata("unknown", None, None), ())[1]

    assert observation.occurred_on is None
    assert observation.amount is None
    assert observation.direction is None
    assert observation.missing_fields == frozenset({"date", "amount", "direction"})
    assert observation.ambiguous_fields == frozenset({"merchant"})
    assert observation.amount_minor is None
    assert observation.blocks_automatic_confirmation is True


def test_unknown_direction_blocks_automatic_confirmation() -> None:
    observation = ParsedObservation(
        occurred_on=date(2026, 8, 15),
        amount=Decimal("4200"),
        currency="KRW",
        direction=TransactionDirection.UNKNOWN,
        merchant="cafe",
    )

    assert observation.blocks_automatic_confirmation is True


def test_installment_counts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least one month"):
        ParsedObservation(
            occurred_on=None,
            amount=None,
            currency=None,
            direction=None,
            merchant=None,
            installment_months=0,
        )
