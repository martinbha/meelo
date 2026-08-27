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
    institution: str
    source_type: str
    image_path: Path | None
    expected_tokens: tuple[ExpectedToken, ...]
    expected_fields: Mapping[str, str]
    expected_rows: tuple[Mapping[str, object], ...]
    expected_failure: str | None


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
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Fixture JSON is invalid at {metadata_path}:{exc.lineno}.") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Fixture manifest must be an object: {metadata_path}.")
        for field in ("name", "institution", "source_type"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ValueError(
                    f"Fixture field '{field}' must be a non-empty string: {metadata_path}."
                )
        raw_tokens = payload.get("tokens", [])
        raw_rows = payload.get("expected_rows", [])
        raw_fields = payload.get("fields", {})
        if (
            not isinstance(raw_tokens, list)
            or not isinstance(raw_rows, list)
            or not isinstance(raw_fields, dict)
        ):
            message = "Fixture tokens and expected_rows must be arrays and fields an object"
            raise ValueError(f"{message}: {metadata_path}.")
        image_path: Path | None = None
        if "image" in payload:
            if not isinstance(payload["image"], str):
                raise ValueError(f"Fixture field 'image' must be a string: {metadata_path}.")
            image_path = (resolved_root / payload["image"]).resolve()
            if not image_path.is_relative_to(resolved_root) or not image_path.is_file():
                raise ValueError(
                    f"Fixture image is missing or outside the fixture root: {metadata_path}."
                )
        if image_path is None and not raw_tokens:
            raise ValueError(f"Fixture requires an image or tokens: {metadata_path}.")
        for index, token in enumerate(raw_tokens):
            if (
                not isinstance(token, dict)
                or not isinstance(token.get("text"), str)
                or not isinstance(token.get("bounds"), list)
                or len(token["bounds"]) != 4
                or any(not isinstance(value, int) for value in token["bounds"])
            ):
                message = f"Fixture tokens[{index}] requires text and four integer bounds"
                raise ValueError(f"{message}: {metadata_path}.")
        for index, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                raise ValueError(
                    f"Fixture expected_rows[{index}] must be an object: {metadata_path}."
                )
            confidence = row.get("confidence")
            minimum = confidence.get("min") if isinstance(confidence, dict) else None
            maximum = confidence.get("max") if isinstance(confidence, dict) else None
            if confidence is not None and (
                not isinstance(confidence, dict)
                or not isinstance(minimum, int | float)
                or not isinstance(maximum, int | float)
                or not 0 <= minimum <= maximum <= 1
            ):
                message = (
                    f"Fixture expected_rows[{index}].confidence must contain a 0..1 min/max range"
                )
                raise ValueError(f"{message}: {metadata_path}.")
        failure = payload.get("expected_failure")
        if failure is not None and (
            not isinstance(failure, dict) or not isinstance(failure.get("code"), str)
        ):
            raise ValueError(f"Fixture expected_failure.code must be a string: {metadata_path}.")
        cases.append(
            OcrFixtureCase(
                name=str(payload["name"]),
                institution=str(payload["institution"]),
                source_type=str(payload["source_type"]),
                image_path=image_path,
                expected_tokens=tuple(
                    ExpectedToken(
                        text=str(token["text"]),
                        bounding_box=BoundingBox(*token["bounds"]),
                    )
                    for token in raw_tokens
                ),
                expected_fields={str(key): str(value) for key, value in raw_fields.items()},
                expected_rows=tuple(dict(row) for row in raw_rows),
                expected_failure=failure["code"] if failure is not None else None,
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
        if case.image_path is None:
            continue
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
        for token in group.tokens:
            token_box = token.bounding_box
            engine_color = "#2563eb" if token.engine == "paddleocr" else "#7c3aed"
            draw.rectangle(
                (token_box.left, token_box.top, token_box.right, token_box.bottom),
                outline=engine_color,
                width=1,
            )
            draw.text(
                (token_box.left, max(0, token_box.top - 10)),
                f"{token.engine} {token.confidence:.2f}",
                fill=engine_color,
            )
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path
