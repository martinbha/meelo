"""Near-identical screenshot detection.

Exact duplicate files are already caught by the SHA-256 of their bytes. This
module catches the ones that differ only by crop, recompression, or a small UI
change, using a difference hash — small, deterministic, and cheap.

The feature is optional. Turning it off leaves exact SHA-256 detection working
exactly as before, because nothing else depends on the perceptual hash.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from PIL import Image, UnidentifiedImageError

ALGORITHM = "dhash8"
#: Width of the reduced image. The hash compares each pixel with its right-hand
#: neighbour, so an 8x8 hash needs a 9-pixel-wide sample.
HASH_SIZE = 8

#: Hamming distance at or below which two screenshots are near-identical. Eight
#: bits out of sixty-four tolerates recompression and small UI shifts while
#: still separating unrelated screens.
DEFAULT_DISTANCE_THRESHOLD = 8
#: Distances above this are not worth recording at all.
MAXIMUM_RECORDED_DISTANCE = 16


class PerceptualHashError(ValueError):
    """The image could not be hashed."""


@dataclass(frozen=True, slots=True)
class SimilarPair:
    """Two documents whose hashes are close enough to be worth showing."""

    document_id: object
    similar_document_id: object
    distance: int
    algorithm: str = ALGORITHM


def near_duplicate_detection_enabled() -> bool:
    """Whether perceptual hashing is switched on for this deployment."""

    return bool(getattr(settings, "NEAR_DUPLICATE_DETECTION_ENABLED", True))


def distance_threshold() -> int:
    return int(getattr(settings, "NEAR_DUPLICATE_DISTANCE_THRESHOLD", DEFAULT_DISTANCE_THRESHOLD))


def perceptual_hash(image_path: str | Path) -> str:
    """A difference hash of one image, as a 16-character hex string.

    Only the hash is kept. The image itself stays under its retention policy,
    so similarity metadata never becomes a reason to hold an image longer.
    """

    try:
        with Image.open(image_path) as source:
            reduced = source.convert("L").resize(
                (HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS
            )
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise PerceptualHashError(f"The image could not be hashed: {exc}") from exc

    # ``getdata`` is deprecated in Pillow 14; the flattened list is the
    # same greyscale sequence either way.
    flatten = getattr(reduced, "get_flattened_data", None)
    pixels = list(flatten() if flatten is not None else reduced.getdata())
    bits = 0
    for row in range(HASH_SIZE):
        offset = row * (HASH_SIZE + 1)
        for column in range(HASH_SIZE):
            left = pixels[offset + column]
            right = pixels[offset + column + 1]
            bits = (bits << 1) | int(left > right)
    return f"{bits:016x}"


def hamming_distance(left: str, right: str) -> int:
    """How many bits two hashes differ by."""

    if not left or not right:
        raise PerceptualHashError("Both hashes are required to compare images.")
    if len(left) != len(right):
        raise PerceptualHashError("Perceptual hashes must be the same length.")
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError as exc:
        raise PerceptualHashError("Perceptual hashes must be hexadecimal.") from exc


def is_near_duplicate(left: str, right: str, *, threshold: int | None = None) -> bool:
    limit = distance_threshold() if threshold is None else threshold
    return hamming_distance(left, right) <= limit


def find_similar(
    *,
    document_id: object,
    image_hash: str,
    others: Iterable[tuple[object, str]],
    threshold: int | None = None,
) -> tuple[SimilarPair, ...]:
    """Compare one hash against others, returning the close ones.

    Unrelated screenshots simply fall outside the threshold; nothing here
    merges anything, it only reports proximity.
    """

    if not near_duplicate_detection_enabled():
        return ()
    limit = distance_threshold() if threshold is None else threshold
    pairs: list[SimilarPair] = []
    for other_id, other_hash in others:
        if other_id == document_id or not other_hash:
            continue
        distance = hamming_distance(image_hash, other_hash)
        if distance <= min(limit, MAXIMUM_RECORDED_DISTANCE):
            pairs.append(SimilarPair(document_id, other_id, distance))
    pairs.sort(key=lambda pair: (pair.distance, str(pair.similar_document_id)))
    return tuple(pairs)


def sorted_pair(left: object, right: object) -> Sequence[object]:
    """Order a pair deterministically so a link is stored once, not twice."""

    return sorted((left, right), key=str)
