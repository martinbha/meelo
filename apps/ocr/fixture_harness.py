from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageDraw

from .contracts import BoundingBox, OcrRunResult
from .matching import MatchStatus, TokenGroup


@dataclass(frozen=True, slots=True)
class ExpectedToken:
    text: str
    bounding_box: BoundingBox


@dataclass(frozen=True, slots=True)
class OcrFixtureCase:
    name: str
    image_path: Path
    expected_tokens: tuple[ExpectedToken, ...]
    expected_fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class FixtureMetrics:
    name: str
    duration_ms: int
    field_accuracy: Mapping[str, bool]

    @property
    def accuracy(self) -> float:
        return (
            sum(self.field_accuracy.values()) / len(self.field_accuracy)
            if self.field_accuracy
            else 1.0
        )


def load_fixture_cases(root: Path) -> tuple[OcrFixtureCase, ...]:
    resolved_root = root.resolve()
    cases: list[OcrFixtureCase] = []
    for metadata_path in sorted(resolved_root.glob("*.json")):
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        image_path = (resolved_root / payload["image"]).resolve()
        if not image_path.is_relative_to(resolved_root) or not image_path.is_file():
            message = f"Fixture image is missing or outside the fixture root: {metadata_path}"
            raise ValueError(message)
        cases.append(
            OcrFixtureCase(
                name=str(payload["name"]),
                image_path=image_path,
                expected_tokens=tuple(
                    ExpectedToken(
                        text=str(token["text"]),
                        bounding_box=BoundingBox(*token["bounds"]),
                    )
                    for token in payload.get("tokens", [])
                ),
                expected_fields={
                    str(key): str(value) for key, value in payload.get("fields", {}).items()
                },
            )
        )
    return tuple(cases)


def run_fixture_suite(
    cases: Sequence[OcrFixtureCase],
    *,
    runner: Callable[[Path], OcrRunResult],
    field_extractor: Callable[[OcrRunResult], Mapping[str, str]],
) -> tuple[FixtureMetrics, ...]:
    metrics: list[FixtureMetrics] = []
    for case in cases:
        result = runner(case.image_path)
        observed = field_extractor(result)
        accuracy = {
            field: observed.get(field) == expected
            for field, expected in case.expected_fields.items()
        }
        metrics.append(FixtureMetrics(case.name, result.duration_ms, accuracy))
    return tuple(metrics)


def render_debug_overlay(
    image_path: Path,
    groups: Sequence[TokenGroup],
    output_path: Path,
) -> Path:
    if not settings.DEBUG:
        raise PermissionError("OCR debug overlays are disabled outside development.")
    colors = {
        MatchStatus.MATCHED: "#1f9d55",
        MatchStatus.UNMATCHED: "#d97706",
        MatchStatus.CONFLICT: "#dc2626",
    }
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    for group in groups:
        box = group.region
        draw.rectangle(
            (box.left, box.top, box.right, box.bottom),
            outline=colors[group.status],
            width=2,
        )
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path
