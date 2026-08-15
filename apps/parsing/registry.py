from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .contracts import (
    DocumentMetadata,
    NormalizedToken,
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
)


class ParserSelectionError(RuntimeError):
    """No registered parser can satisfy an explicit selection."""


@dataclass(frozen=True, slots=True)
class ParserSelection:
    metadata: ParserMetadata
    support: ParserSupport
    observations: tuple[ParsedObservation, ...]


class ParserRegistry:
    def __init__(self, *, generic_parser: ScreenshotParser) -> None:
        self._generic = generic_parser
        self._parsers: dict[str, ScreenshotParser] = {generic_parser.metadata.name: generic_parser}

    def register(self, parser: ScreenshotParser) -> None:
        name = parser.metadata.name
        if name in self._parsers:
            raise ValueError(f"A parser named '{name}' is already registered.")
        self._parsers[name] = parser

    def parsers(self) -> tuple[ScreenshotParser, ...]:
        return tuple(self._parsers[name] for name in sorted(self._parsers))

    def select(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ScreenshotParser, ParserSupport]:
        if document.manual_source_override:
            parser = self._parsers.get(document.manual_source_override)
            if parser is None:
                raise ParserSelectionError(
                    f"The requested parser '{document.manual_source_override}' is unavailable."
                )
            return parser, parser.supports(document, tokens)
        scored = [
            (parser.supports(document, tokens), parser)
            for parser in self._parsers.values()
            if parser is not self._generic
        ]
        supported = [(support, parser) for support, parser in scored if support.score > 0]
        if not supported:
            return self._generic, self._generic.supports(document, tokens)
        support, parser = min(
            supported,
            key=lambda item: (
                -item[0].score,
                item[1].metadata.name,
                item[1].metadata.version,
            ),
        )
        return parser, support

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSelection:
        parser, support = self.select(document, tokens)
        metadata = parser.metadata
        observations = tuple(
            replace(
                observation,
                parser_name=metadata.name,
                parser_version=metadata.version,
                parser_support_score=support.score,
            )
            for observation in parser.parse(document, tokens)
        )
        return ParserSelection(metadata, support, observations)
