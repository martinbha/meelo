from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any

from .contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrRunResult,
    OcrToken,
)

PADDLE_LANGUAGES = frozenset({"en", "ko"})
LANGUAGE_MODEL_NAMES = {"en": "en", "ko": "korean"}


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _default_factory(**options: Any) -> Any:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    try:
        from paddleocr import PaddleOCR  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:
        raise OcrConfigurationError(
            "PaddleOCR is unavailable; install the local Paddle runtime and models."
        ) from exc
    try:
        return PaddleOCR(**options)
    except Exception as exc:
        raise OcrConfigurationError("PaddleOCR model initialization failed.") from exc


def _box(points: Sequence[Sequence[float]]) -> BoundingBox:
    if len(points) < 2:
        raise OcrConfigurationError("PaddleOCR returned an invalid token boundary.")
    xs = [max(0, round(point[0])) for point in points]
    ys = [max(0, round(point[1])) for point in points]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def _result_mapping(page: Any) -> Mapping[str, Any] | None:
    if isinstance(page, Mapping):
        return page
    serialized = getattr(page, "json", None)
    if isinstance(serialized, Mapping):
        result = serialized.get("res", serialized)
        return result if isinstance(result, Mapping) else None
    return None


def _normalized_tokens(output: Any) -> tuple[OcrToken, ...]:
    tokens: list[OcrToken] = []
    pages = output or []
    for page in pages:
        mapping = _result_mapping(page)
        if mapping is not None:
            for text, confidence, points in zip(
                mapping.get("rec_texts", ()),
                mapping.get("rec_scores", ()),
                mapping.get("rec_polys", ()),
                strict=True,
            ):
                tokens.append(
                    OcrToken(
                        text=str(text),
                        confidence=max(0.0, min(1.0, float(confidence))),
                        bounding_box=_box(points),
                    )
                )
            continue
        for item in page or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            points, recognition = item
            if not isinstance(recognition, (list, tuple)) or len(recognition) != 2:
                continue
            text, confidence = recognition
            tokens.append(
                OcrToken(
                    text=str(text),
                    confidence=max(0.0, min(1.0, float(confidence))),
                    bounding_box=_box(points),
                )
            )
    return tuple(tokens)


def _json_safe_output(output: Any) -> Any:
    if isinstance(output, Mapping):
        return {str(key): _json_safe_output(value) for key, value in output.items()}
    if isinstance(output, (list, tuple)):
        return [_json_safe_output(value) for value in output]
    serialized = getattr(output, "json", None)
    if isinstance(serialized, Mapping):
        return _json_safe_output(serialized.get("res", serialized))
    to_list = getattr(output, "tolist", None)
    if callable(to_list):
        return _json_safe_output(to_list())
    if isinstance(output, str | int | float | bool) or output is None:
        return output
    return str(output)


class PaddleOcrEngine(OcrEngine):
    def __init__(self, *, factory: Callable[..., Any] = _default_factory) -> None:
        self._factory = factory

    @property
    def metadata(self) -> EngineMetadata:
        return EngineMetadata(
            engine="paddleocr",
            engine_version=_package_version("paddleocr"),
            model_versions={"paddlepaddle": _package_version("paddlepaddle")},
        )

    @property
    def supported_languages(self) -> frozenset[str]:
        return PADDLE_LANGUAGES

    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        self.validate_configuration(configuration)
        if not image_path.is_file():
            raise OcrConfigurationError("The PaddleOCR input image does not exist.")
        if len(configuration.languages) != 1:
            raise OcrConfigurationError("PaddleOCR runs require exactly one model language.")
        options = dict(configuration.options)
        options.setdefault("lang", LANGUAGE_MODEL_NAMES[configuration.languages[0]])
        options.setdefault("use_doc_orientation_classify", False)
        options.setdefault("use_doc_unwarping", False)
        options.setdefault("use_textline_orientation", False)
        engine = self._factory(**options)
        started = perf_counter()
        try:
            predict = getattr(engine, "predict", None)
            output = (
                predict(str(image_path))
                if callable(predict)
                else engine.ocr(str(image_path), cls=False)
            )
        except Exception as exc:
            raise OcrConfigurationError("PaddleOCR execution failed.") from exc
        duration_ms = round((perf_counter() - started) * 1000)
        metadata = EngineMetadata(
            engine="paddleocr",
            engine_version=_package_version("paddleocr"),
            model_versions={
                "paddlepaddle": _package_version("paddlepaddle"),
                "ocr": str(options.get("ocr_version", "PP-OCRv5")),
                "language": str(options["lang"]),
            },
        )
        return OcrRunResult(
            tokens=_normalized_tokens(output),
            metadata=metadata,
            configuration=configuration,
            duration_ms=duration_ms,
            raw_output=json.dumps(
                _json_safe_output(output), ensure_ascii=False, separators=(",", ":")
            ),
        )
