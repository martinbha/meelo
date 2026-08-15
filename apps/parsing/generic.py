from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median

from apps.ocr.contracts import BoundingBox

from .contracts import (
    DocumentMetadata,
    NormalizedToken,
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    ScreenshotParser,
    TransactionDirection,
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONEY_RE = re.compile(r"^(?P<amount>-?\d+) (?P<currency>[A-Z]{3})$")
DEBIT_MARKERS = frozenset({"debit", "withdrawal", "출금", "결제"})
CREDIT_MARKERS = frozenset({"credit", "deposit", "입금", "환불"})


def _center(token: NormalizedToken) -> float:
    return (token.bounding_box.top + token.bounding_box.bottom) / 2


def _height(token: NormalizedToken) -> int:
    return max(1, token.bounding_box.bottom - token.bounding_box.top)


def _rows(tokens: Sequence[NormalizedToken]) -> tuple[tuple[NormalizedToken, ...], ...]:
    ordered = sorted(
        tokens,
        key=lambda token: (_center(token), token.bounding_box.left, token.sequence),
    )
    if not ordered:
        return ()
    typical_height = float(median(_height(token) for token in ordered))
    threshold = max(4.0, typical_height * 0.7)
    rows: list[list[NormalizedToken]] = []
    centers: list[float] = []
    for token in ordered:
        if not rows or abs(_center(token) - centers[-1]) > threshold:
            rows.append([token])
            centers.append(_center(token))
        else:
            rows[-1].append(token)
            centers[-1] = sum(_center(item) for item in rows[-1]) / len(rows[-1])
    return tuple(
        tuple(sorted(row, key=lambda token: (token.bounding_box.left, token.sequence)))
        for row in rows
    )


def _region(row: Sequence[NormalizedToken]) -> BoundingBox:
    return BoundingBox(
        min(token.bounding_box.left for token in row),
        min(token.bounding_box.top for token in row),
        max(token.bounding_box.right for token in row),
        max(token.bounding_box.bottom for token in row),
    )


def _date_candidates(row: Sequence[NormalizedToken]) -> list[tuple[int, date]]:
    candidates: list[tuple[int, date]] = []
    for index, token in enumerate(row):
        if DATE_RE.fullmatch(token.text):
            try:
                candidates.append((index, date.fromisoformat(token.text)))
            except ValueError:
                continue
    return candidates


def _amount_candidates(row: Sequence[NormalizedToken]) -> list[tuple[int, Decimal, str]]:
    candidates: list[tuple[int, Decimal, str]] = []
    for index, token in enumerate(row):
        match = MONEY_RE.fullmatch(token.text)
        if match is None:
            continue
        try:
            amount = Decimal(match.group("amount"))
        except InvalidOperation:
            continue
        candidates.append((index, amount, match.group("currency")))
    return candidates


def _directions(row: Sequence[NormalizedToken]) -> list[tuple[int, TransactionDirection]]:
    candidates: list[tuple[int, TransactionDirection]] = []
    for index, token in enumerate(row):
        if token.text in DEBIT_MARKERS:
            candidates.append((index, TransactionDirection.DEBIT))
        elif token.text in CREDIT_MARKERS:
            candidates.append((index, TransactionDirection.CREDIT))
    return candidates


class GenericTransactionListParser(ScreenshotParser):
    @property
    def metadata(self) -> ParserMetadata:
        return ParserMetadata("generic", "1.0")

    def supports(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> ParserSupport:
        dates = sum(DATE_RE.fullmatch(token.text) is not None for token in tokens)
        amounts = sum(MONEY_RE.fullmatch(token.text) is not None for token in tokens)
        score = min(0.49, 0.05 + min(dates, amounts) * 0.1)
        return ParserSupport(score, "generic_transaction_list", ("generic fallback",))

    def parse(
        self, document: DocumentMetadata, tokens: Sequence[NormalizedToken]
    ) -> tuple[ParsedObservation, ...]:
        observations: list[ParsedObservation] = []
        for row in _rows(tokens):
            dates = _date_candidates(row)
            amounts = _amount_candidates(row)
            directions = _directions(row)
            ambiguous: set[str] = set()
            if len(dates) > 1:
                ambiguous.add("date")
            if len(amounts) > 1:
                ambiguous.add("amount")
            if len({direction for _, direction in directions}) > 1:
                ambiguous.add("direction")
            occurred_on = dates[0][1] if len(dates) == 1 else None
            amount = amounts[0][1] if len(amounts) == 1 else None
            currency = amounts[0][2] if len(amounts) == 1 else None
            direction = directions[0][1] if directions and "direction" not in ambiguous else None
            consumed = {index for index, *_ in (*dates, *amounts, *directions)}
            merchant_tokens = [
                token.text for index, token in enumerate(row) if index not in consumed
            ]
            merchant = " ".join(merchant_tokens).strip() or None
            missing = {
                field
                for field, value in {
                    "date": occurred_on,
                    "amount": amount,
                    "direction": direction,
                    "merchant": merchant,
                }.items()
                if value is None and field not in ambiguous
            }
            average_confidence = sum(token.confidence for token in row) / len(row)
            requires_review = bool(missing or ambiguous)
            observations.append(
                ParsedObservation(
                    occurred_on=occurred_on,
                    amount=amount,
                    currency=currency,
                    direction=direction,
                    merchant=merchant,
                    source_region=_region(row),
                    confidence_factors={
                        "token_confidence": round(average_confidence, 6),
                        "row_token_count": len(row),
                        "requires_review": requires_review,
                    },
                    missing_fields=frozenset(missing),
                    ambiguous_fields=frozenset(ambiguous),
                )
            )
        return tuple(observations)
