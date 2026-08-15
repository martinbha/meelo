from collections.abc import Sequence

import pytest

from apps.parsing.contracts import (
    DocumentMetadata,
    NormalizedToken,
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
)
from apps.parsing.registry import ParserRegistry, ParserSelectionError


class ScoredParser(ScreenshotParser):
    def __init__(self, name: str, score: float, source_type: str = "unknown") -> None:
        self._metadata = ParserMetadata(name, "1.0")
        self.score = score
        self.source_type = source_type

    @property
    def metadata(self) -> ParserMetadata:
        return self._metadata

    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        return ParserSupport(self.score, self.source_type, (self.metadata.name,))

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        return (ParsedObservation(None, None, None, None, self.metadata.name),)


def test_registry_selects_highest_score_and_stamps_observations() -> None:
    registry = ParserRegistry(generic_parser=ScoredParser("generic", 0.1))
    registry.register(ScoredParser("bank", 0.8, "bank_list"))
    registry.register(ScoredParser("card", 0.9, "card_list"))

    selection = registry.parse(DocumentMetadata("unknown", None, None), ())

    assert selection.metadata.name == "card"
    assert selection.support.detected_source_type == "card_list"
    assert selection.observations[0].parser_name == "card"
    assert selection.observations[0].parser_version == "1.0"
    assert selection.observations[0].parser_support_score == 0.9


def test_registry_ties_are_deterministic_and_unknown_falls_back() -> None:
    generic = ScoredParser("generic", 0.05)
    registry = ParserRegistry(generic_parser=generic)
    registry.register(ScoredParser("zeta", 0.7))
    registry.register(ScoredParser("alpha", 0.7))

    assert registry.select(DocumentMetadata("unknown", None, None), ())[0].metadata.name == (
        "alpha"
    )
    registry = ParserRegistry(generic_parser=generic)
    registry.register(ScoredParser("unsupported", 0.0))
    assert registry.select(DocumentMetadata("unknown", None, None), ())[0] is generic


def test_registry_honors_manual_override_and_rejects_unknown_override() -> None:
    registry = ParserRegistry(generic_parser=ScoredParser("generic", 0.1))
    forced = ScoredParser("forced", 0.2)
    registry.register(forced)

    selected, _ = registry.select(
        DocumentMetadata("unknown", None, None, manual_source_override="forced"), ()
    )
    assert selected is forced
    with pytest.raises(ParserSelectionError, match="missing"):
        registry.select(
            DocumentMetadata("unknown", None, None, manual_source_override="missing"), ()
        )
