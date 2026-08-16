"""Interpret deposit, withdrawal, approval, payment, and refund labels.

The same words mean different things depending on what the screenshot shows.
``결제`` on a card transaction list is a purchase that increases card debt; the
same word on a credit-card payment receipt is the monthly settlement that
reduces it. Direction is therefore always resolved against the source type, and
the sign printed on screen is kept separate from the economic direction it
implies.

Anything the tables below cannot explain resolves to
:attr:`TransactionDirection.UNKNOWN`, which blocks automatic confirmation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .contracts import TransactionDirection

DEBIT = TransactionDirection.DEBIT
CREDIT = TransactionDirection.CREDIT
UNKNOWN = TransactionDirection.UNKNOWN


class SourceCategory(StrEnum):
    """How a screenshot's labels and signs should be read."""

    BANK = "bank"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    CREDIT_CARD_STATEMENT = "credit_card_statement"
    CREDIT_CARD_PAYMENT = "credit_card_payment"
    UNKNOWN = "unknown"


_SOURCE_TYPE_CATEGORIES: dict[str, SourceCategory] = {
    "bank_transaction_list": SourceCategory.BANK,
    "bank_transaction_detail": SourceCategory.BANK,
    "bank_transfer_confirmation": SourceCategory.BANK,
    "credit_card_statement": SourceCategory.CREDIT_CARD_STATEMENT,
    "credit_card_payment": SourceCategory.CREDIT_CARD_PAYMENT,
}

_CARD_SOURCE_TYPES = frozenset({"card_transaction_list", "card_transaction_detail"})

_INSTRUMENT_CATEGORIES: dict[str, SourceCategory] = {
    "debit_card": SourceCategory.DEBIT_CARD,
    "credit_card": SourceCategory.CREDIT_CARD,
    "virtual_card": SourceCategory.CREDIT_CARD,
    "prepaid_card": SourceCategory.DEBIT_CARD,
}


def source_category(source_type: str, *, instrument_type: str | None = None) -> SourceCategory:
    """Map a document source type, refined by the instrument, to a category."""

    normalized = (source_type or "").strip().casefold()
    if normalized in _CARD_SOURCE_TYPES:
        if instrument_type is None:
            return SourceCategory.CREDIT_CARD
        return _INSTRUMENT_CATEGORIES.get(
            instrument_type.strip().casefold(), SourceCategory.CREDIT_CARD
        )
    return _SOURCE_TYPE_CATEGORIES.get(normalized, SourceCategory.UNKNOWN)


#: Money-out labels. Their economic direction still depends on the source.
_OUTFLOW_LABELS = (
    "출금",
    "인출",
    "결제",
    "승인",
    "지불",
    "withdrawal",
    "withdraw",
    "payment",
    "paid",
    "approved",
    "approval",
    "purchase",
    "debit",
)

#: Money-in labels.
_INFLOW_LABELS = (
    "입금",
    "환불",
    "취소",
    "승인취소",
    "반품",
    "deposit",
    "refund",
    "refunded",
    "cancelled",
    "canceled",
    "cancellation",
    "reversal",
    "credit",
)

#: Labels that name a settlement of a card balance.
_SETTLEMENT_LABELS = (
    "납부",
    "청구",
    "카드대금",
    "대금결제",
    "자동이체",
    "결제일",
    "statement",
    "settlement",
    "billed",
    "autopay",
)

#: Labels that genuinely do not say which way money moved.
_AMBIVALENT_LABELS = (
    "이체",
    "송금",
    "transfer",
    "remittance",
)

#: Card sources never show a customer deposit, so an outflow label on a card
#: purchase screen is a debit regardless of how the amount is signed.
_CARD_CATEGORIES = frozenset(
    {
        SourceCategory.DEBIT_CARD,
        SourceCategory.CREDIT_CARD,
        SourceCategory.CREDIT_CARD_STATEMENT,
    }
)

#: Every recognised label, for callers that need to tell a label apart from a
#: counterparty name.
ALL_LABELS: frozenset[str] = frozenset(
    _SETTLEMENT_LABELS + _INFLOW_LABELS + _OUTFLOW_LABELS + _AMBIVALENT_LABELS
)

CONFIDENCE_LABELLED = 0.95
CONFIDENCE_SIGNED = 0.75
CONFIDENCE_SOURCE_DEFAULT = 0.6
CONFIDENCE_UNKNOWN = 0.0


@dataclass(frozen=True, slots=True)
class DirectionResolution:
    """An economic direction plus the evidence that produced it."""

    direction: TransactionDirection
    source_label: str | None
    display_sign: str
    confidence: float
    reasons: tuple[str, ...] = ()
    is_settlement: bool = False

    @property
    def is_unknown(self) -> bool:
        return self.direction is UNKNOWN

    @property
    def blocks_automatic_confirmation(self) -> bool:
        """Unknown directions always require a human decision."""

        return self.is_unknown


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _find_label(texts: Sequence[str]) -> tuple[str | None, str | None]:
    """Return the first recognized label and the family it belongs to."""

    families = (
        (_SETTLEMENT_LABELS, "settlement"),
        (_INFLOW_LABELS, "inflow"),
        (_OUTFLOW_LABELS, "outflow"),
        (_AMBIVALENT_LABELS, "ambivalent"),
    )
    for text in texts:
        cleaned = _clean(text)
        if not cleaned:
            continue
        for labels, family in families:
            for label in labels:
                if label in cleaned:
                    return label, family
    return None, None


def is_direction_label(text: str) -> bool:
    """Whether a token is only a direction label, not a counterparty name."""

    cleaned = _clean(text)
    if not cleaned:
        return False
    return cleaned in ALL_LABELS or cleaned.replace(" ", "") in ALL_LABELS


def _from_sign(display_sign: str, category: SourceCategory) -> TransactionDirection:
    """Interpret a display sign, which only banks use consistently."""

    if category is SourceCategory.BANK and display_sign in {"-", "+"}:
        return DEBIT if display_sign == "-" else CREDIT
    if category in _CARD_CATEGORIES and display_sign == "-":
        # A negative amount on a card screen is a cancellation or refund.
        return CREDIT
    return UNKNOWN


def _settlement_direction(category: SourceCategory) -> TransactionDirection:
    """A card settlement is a credit to the card and a debit to the bank."""

    if category in {SourceCategory.CREDIT_CARD_PAYMENT, SourceCategory.CREDIT_CARD_STATEMENT}:
        return CREDIT
    if category is SourceCategory.BANK:
        return DEBIT
    return UNKNOWN


def resolve_direction(
    *,
    source_type: str,
    labels: Sequence[str] = (),
    display_sign: str = "",
    instrument_type: str | None = None,
) -> DirectionResolution:
    """Resolve the economic direction of one parsed row.

    ``display_sign`` is the sign printed on the screen (``"-"``, ``"+"``, or
    ``""``) and is reported back unchanged so review can see what the source
    actually showed.
    """

    category = source_category(source_type, instrument_type=instrument_type)
    label, family = _find_label(labels)
    reasons: list[str] = [f"source_category={category.value}"]

    if family == "settlement":
        direction = _settlement_direction(category)
        reasons.append(f"settlement_label={label}")
        if direction is UNKNOWN:
            reasons.append("settlement direction depends on an unidentified source")
            return DirectionResolution(
                UNKNOWN, label, display_sign, CONFIDENCE_UNKNOWN, tuple(reasons), True
            )
        return DirectionResolution(
            direction, label, display_sign, CONFIDENCE_LABELLED, tuple(reasons), True
        )

    if family in {"inflow", "outflow"}:
        if category is SourceCategory.CREDIT_CARD_PAYMENT:
            # Every amount on a payment receipt settles the card.
            reasons.append(f"payment_receipt_label={label}")
            return DirectionResolution(
                CREDIT, label, display_sign, CONFIDENCE_LABELLED, tuple(reasons), True
            )
        direction = DEBIT if family == "outflow" else CREDIT
        reasons.append(f"{family}_label={label}")
        return DirectionResolution(
            direction, label, display_sign, CONFIDENCE_LABELLED, tuple(reasons)
        )

    if family == "ambivalent":
        reasons.append(f"ambivalent_label={label}")
        signed = _from_sign(display_sign, category)
        if signed is not UNKNOWN:
            reasons.append(f"display_sign={display_sign}")
            return DirectionResolution(
                signed, label, display_sign, CONFIDENCE_SIGNED, tuple(reasons)
            )
        reasons.append("transfer direction is not stated")
        return DirectionResolution(UNKNOWN, label, display_sign, CONFIDENCE_UNKNOWN, tuple(reasons))

    signed = _from_sign(display_sign, category)
    if signed is not UNKNOWN:
        reasons.append(f"display_sign={display_sign}")
        return DirectionResolution(signed, None, display_sign, CONFIDENCE_SIGNED, tuple(reasons))

    if category in {SourceCategory.DEBIT_CARD, SourceCategory.CREDIT_CARD}:
        # An unsigned, unlabelled amount on a card purchase screen is a purchase.
        reasons.append("card purchase screens default to debit")
        return DirectionResolution(
            DEBIT, None, display_sign, CONFIDENCE_SOURCE_DEFAULT, tuple(reasons)
        )
    if category is SourceCategory.CREDIT_CARD_PAYMENT:
        reasons.append("payment receipts default to a card credit")
        return DirectionResolution(
            CREDIT, None, display_sign, CONFIDENCE_SOURCE_DEFAULT, tuple(reasons), True
        )

    reasons.append("no direction label or sign was found")
    return DirectionResolution(UNKNOWN, None, display_sign, CONFIDENCE_UNKNOWN, tuple(reasons))
