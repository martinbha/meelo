from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from apps.ocr.contracts import BoundingBox

PARSER_OUTPUT_VERSION = 1


class TransactionDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


@dataclass(frozen=True, slots=True)
class NormalizedToken:
    text: str
    confidence: float
    bounding_box: BoundingBox
    sequence: int
    source_engines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Normalized token confidence must be between zero and one.")


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    source_type: str
    width: int | None
    height: int | None
    institution_hint: str | None = None
    manual_source_override: str | None = None


@dataclass(frozen=True, slots=True)
class ParserMetadata:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ParserSupport:
    score: float
    detected_source_type: str
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("Parser support score must be between zero and one.")


@dataclass(frozen=True, slots=True)
class ParsedObservation:
    occurred_on: date | None
    amount: Decimal | None
    currency: str | None
    direction: TransactionDirection | None
    merchant: str | None
    instrument_suffix: str | None = None
    balance_after: Decimal | None = None
    source_region: BoundingBox | None = None
    confidence_factors: Mapping[str, float | bool | str] = field(default_factory=dict)
    missing_fields: frozenset[str] = frozenset()
    ambiguous_fields: frozenset[str] = frozenset()
    output_version: int = PARSER_OUTPUT_VERSION
    parser_name: str = ""
    parser_version: str = ""
    parser_support_score: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "confidence_factors", MappingProxyType(dict(self.confidence_factors))
        )
        if self.output_version < 1:
            raise ValueError("Parser output versions must be positive.")
        if not 0.0 <= self.parser_support_score <= 1.0:
            raise ValueError("Stored parser support score must be between zero and one.")


class ScreenshotParser(ABC):
    @property
    @abstractmethod
    def metadata(self) -> ParserMetadata:
        raise NotImplementedError

    @abstractmethod
    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        raise NotImplementedError

    @abstractmethod
    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        raise NotImplementedError
