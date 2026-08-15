from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz.fuzz import ratio

from .contracts import BoundingBox, OcrToken
from .normalization import normalize_ocr_text

STRUCTURED_VALUE_RE = re.compile(r"^(?:-?\d+ KRW|\d{4}-\d{2}-\d{2})$")


class MatchStatus(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class TokenCandidate:
    engine: str
    text: str
    normalized_text: str
    confidence: float
    bounding_box: BoundingBox

    @classmethod
    def from_token(cls, engine: str, token: OcrToken) -> TokenCandidate:
        return cls(
            engine=engine,
            text=token.text,
            normalized_text=normalize_ocr_text(token.text),
            confidence=token.confidence,
            bounding_box=token.bounding_box,
        )


@dataclass(frozen=True, slots=True)
class TokenGroup:
    status: MatchStatus
    tokens: tuple[TokenCandidate, ...]
    region: BoundingBox
    text_similarity: float


def _area(box: BoundingBox) -> int:
    return max(0, box.right - box.left) * max(0, box.bottom - box.top)


def overlap_ratio(first: BoundingBox, second: BoundingBox) -> float:
    width = max(0, min(first.right, second.right) - max(first.left, second.left))
    height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    intersection = width * height
    smallest = min(_area(first), _area(second))
    return intersection / smallest if smallest else 0.0


def vertical_center_distance(first: BoundingBox, second: BoundingBox) -> float:
    first_center = (first.top + first.bottom) / 2
    second_center = (second.top + second.bottom) / 2
    scale = max(first.bottom - first.top, second.bottom - second.top, 1)
    return abs(first_center - second_center) / scale


def horizontal_placement_distance(first: BoundingBox, second: BoundingBox) -> float:
    first_center = (first.left + first.right) / 2
    second_center = (second.left + second.right) / 2
    scale = max(first.right - first.left, second.right - second.left, 1)
    return abs(first_center - second_center) / scale


def _spatially_compatible(first: BoundingBox, second: BoundingBox) -> bool:
    return overlap_ratio(first, second) >= 0.25 or (
        vertical_center_distance(first, second) <= 0.55
        and horizontal_placement_distance(first, second) <= 0.75
    )


def _union(tokens: tuple[TokenCandidate, ...]) -> BoundingBox:
    return BoundingBox(
        min(token.bounding_box.left for token in tokens),
        min(token.bounding_box.top for token in tokens),
        max(token.bounding_box.right for token in tokens),
        max(token.bounding_box.bottom for token in tokens),
    )


def match_engine_tokens(
    primary: tuple[TokenCandidate, ...],
    secondary: tuple[TokenCandidate, ...],
    *,
    fuzzy_threshold: float = 82.0,
) -> tuple[TokenGroup, ...]:
    available = set(range(len(secondary)))
    groups: list[TokenGroup] = []
    for candidate in primary:
        spatial = [
            index
            for index in available
            if _spatially_compatible(candidate.bounding_box, secondary[index].bounding_box)
        ]
        if not spatial:
            unmatched_tokens = (candidate,)
            groups.append(
                TokenGroup(
                    MatchStatus.UNMATCHED,
                    unmatched_tokens,
                    _union(unmatched_tokens),
                    0.0,
                )
            )
            continue
        best_index = max(
            spatial,
            key=lambda index: (
                ratio(candidate.normalized_text, secondary[index].normalized_text),
                overlap_ratio(candidate.bounding_box, secondary[index].bounding_box),
            ),
        )
        available.remove(best_index)
        other = secondary[best_index]
        similarity = float(ratio(candidate.normalized_text, other.normalized_text))
        structured_disagreement = (
            candidate.normalized_text != other.normalized_text
            and (
                STRUCTURED_VALUE_RE.fullmatch(candidate.normalized_text) is not None
                or STRUCTURED_VALUE_RE.fullmatch(other.normalized_text) is not None
            )
        )
        status = (
            MatchStatus.MATCHED
            if similarity >= fuzzy_threshold and not structured_disagreement
            else MatchStatus.CONFLICT
        )
        matched_tokens = (candidate, other)
        groups.append(TokenGroup(status, matched_tokens, _union(matched_tokens), similarity))
    for index in sorted(available):
        unmatched_tokens = (secondary[index],)
        groups.append(
            TokenGroup(
                MatchStatus.UNMATCHED,
                unmatched_tokens,
                _union(unmatched_tokens),
                0.0,
            )
        )
    return tuple(
        sorted(
            groups,
            key=lambda group: (group.region.top, group.region.left, group.status.value),
        )
    )
