"""Group OCR tokens into visual rows using their coordinates.

Every parser starts from the same problem: OCR returns a flat token list, but a
transaction is a horizontal band of tokens. Row grouping is shared so the
generic parser and the institution parsers agree on what a row is.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from apps.ocr.contracts import BoundingBox

from .contracts import NormalizedToken

#: Tokens are treated as one row while their vertical centres stay within this
#: fraction of the typical glyph height.
ROW_HEIGHT_RATIO = 0.7
MINIMUM_ROW_THRESHOLD = 4.0


def token_center(token: NormalizedToken) -> float:
    return (token.bounding_box.top + token.bounding_box.bottom) / 2


def token_height(token: NormalizedToken) -> int:
    return max(1, token.bounding_box.bottom - token.bounding_box.top)


def group_rows(tokens: Sequence[NormalizedToken]) -> tuple[tuple[NormalizedToken, ...], ...]:
    """Group tokens into top-to-bottom rows, each ordered left to right."""

    ordered = sorted(
        tokens,
        key=lambda token: (token_center(token), token.bounding_box.left, token.sequence),
    )
    if not ordered:
        return ()
    typical_height = float(median(token_height(token) for token in ordered))
    threshold = max(MINIMUM_ROW_THRESHOLD, typical_height * ROW_HEIGHT_RATIO)
    rows: list[list[NormalizedToken]] = []
    centers: list[float] = []
    for token in ordered:
        if not rows or abs(token_center(token) - centers[-1]) > threshold:
            rows.append([token])
            centers.append(token_center(token))
        else:
            rows[-1].append(token)
            centers[-1] = sum(token_center(item) for item in rows[-1]) / len(rows[-1])
    return tuple(
        tuple(sorted(row, key=lambda token: (token.bounding_box.left, token.sequence)))
        for row in rows
    )


def row_region(row: Sequence[NormalizedToken]) -> BoundingBox:
    """The bounding box covering every token in a row."""

    return BoundingBox(
        min(token.bounding_box.left for token in row),
        min(token.bounding_box.top for token in row),
        max(token.bounding_box.right for token in row),
        max(token.bounding_box.bottom for token in row),
    )


def average_confidence(row: Sequence[NormalizedToken]) -> float:
    if not row:
        return 0.0
    return sum(token.confidence for token in row) / len(row)
