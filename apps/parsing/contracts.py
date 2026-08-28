from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from apps.core.value_objects import InvalidMoneyError, Money
from apps.ocr.contracts import BoundingBox

#: Version 2 added the document dating context, the source sign and label, and
#: the card metadata institution parsers emit.
PARSER_OUTPUT_VERSION = 2


class TransactionDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    #: The screen carried no label the parser could interpret for its source
    #: type. Unknown is recorded explicitly rather than guessed, and it blocks
    #: automatic confirmation.
    UNKNOWN = "unknown"


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
    #: Upload moment and local time zone, used to date relative and partial
    #: rows. Parsers fall back to explicit dates only when these are absent.
    uploaded_at: datetime | None = None
    time_zone: str = ""
    #: The statement month a document covers, when the user or the screen
    #: states it. It outranks the upload date for year inference.
    statement_month: date | None = None
    #: The instrument the document belongs to, which decides how card signs
    #: and labels are read.
    instrument_type: str | None = None


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
    counterparty: str | None = None
    instrument_suffix: str | None = None
    balance_after: Decimal | None = None
    balance_before: Decimal | None = None
    source_region: BoundingBox | None = None
    confidence_factors: Mapping[str, float | bool | str] = field(default_factory=dict)
    missing_fields: frozenset[str] = frozenset()
    ambiguous_fields: frozenset[str] = frozenset()
    #: The sign and label printed on the screen, kept apart from ``direction``
    #: so review can see what the source actually showed.
    display_sign: str = ""
    direction_label: str | None = None
    #: Card metadata. ``installment_months`` is 1 for a single payment.
    approval_code: str | None = None
    installment_months: int | None = None
    #: True when the row settles a card balance rather than buying something.
    is_settlement: bool = False
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
        if self.installment_months is not None and self.installment_months < 1:
            raise ValueError("Installment counts must be at least one month.")

    @property
    def amount_minor(self) -> int | None:
        """The amount in integer minor units, the form storage and reports use."""

        if self.amount is None or self.currency is None:
            return None
        try:
            return Money.from_decimal(self.amount, self.currency).amount_minor
        except InvalidMoneyError:  # pragma: no cover - parsers emit valid decimals
            return None

    @property
    def blocks_automatic_confirmation(self) -> bool:
        """Rows a human must look at before they can become transactions."""

        return bool(
            self.missing_fields
            or self.ambiguous_fields
            or self.direction is None
            or self.direction is TransactionDirection.UNKNOWN
        )


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """A card statement summary kept separate from transaction candidates."""

    period_start: date
    period_end: date
    due_date: date
    total_minor: int
    currency: str
    summary: ParsedObservation
    line_items: tuple[ParsedObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedCardPayment:
    """A card-balance payment ready for settlement matching."""

    issuer: str
    occurred_on: date
    amount_minor: int
    currency: str
    instrument_suffix: str | None
    summary: ParsedObservation


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
