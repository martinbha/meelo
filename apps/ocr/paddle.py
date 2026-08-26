from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
    OcrEngineError,
    OcrRunResult,
    OcrToken,
)

PADDLE_LANGUAGES = frozenset({"en", "ko"})
LANGUAGE_MODEL_NAMES = {"en": "en", "ko": "korean"}
MODEL_MANIFEST_NAME = "manifest.json"
MODEL_ROOT_ENV = "PADDLE_OCR_MODEL_ROOT"


@dataclass(frozen=True, slots=True)
class PaddleModelAssets:
    detection_name: str
    detection_dir: Path
    detection_digest: str
    recognition_name: str
    recognition_dir: Path
    recognition_digest: str
    ocr_version: str

    @property
    def versions(self) -> dict[str, str]:
        return {
            "ocr": self.ocr_version,
            "detection": f"{self.detection_name}@sha256:{self.detection_digest}",
            "recognition": f"{self.recognition_name}@sha256:{self.recognition_digest}",
        }


def _local_assets(language: str) -> PaddleModelAssets:
    configured = os.environ.get(MODEL_ROOT_ENV, "")
    root = Path(configured) if configured else Path("/nonexistent/paddle-models")
    try:
        payload = json.loads((root / MODEL_MANIFEST_NAME).read_text(encoding="utf-8"))
        detection = payload["detection"]
        recognition = payload["recognition"][language]
        assets = PaddleModelAssets(
            detection_name=str(detection["name"]),
            detection_dir=root / str(detection["directory"]),
            detection_digest=str(detection["sha256"]),
            recognition_name=str(recognition["name"]),
            recognition_dir=root / str(recognition["directory"]),
            recognition_digest=str(recognition["sha256"]),
            ocr_version=str(payload["ocr_version"]),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OcrEngineError(
            "Pinned PaddleOCR model assets are missing or invalid.",
            code="PADDLEOCR_FAILED",
            retryable=False,
        ) from exc
    if (
        not assets.detection_dir.is_dir()
        or not assets.recognition_dir.is_dir()
        or _model_digest(assets.detection_dir) != assets.detection_digest
        or _model_digest(assets.recognition_dir) != assets.recognition_digest
    ):
        raise OcrEngineError(
            "Pinned PaddleOCR model assets are missing or invalid.",
            code="PADDLEOCR_FAILED",
            retryable=False,
        )
    return assets


def _model_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and ".cache" not in path.parts
    )
    if not files:
        return ""
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


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
        raise OcrEngineError(
            "PaddleOCR model initialization failed.",
            code="PADDLEOCR_FAILED",
            retryable=False,
        ) from exc


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
    engine_name = "paddleocr"

    def __init__(self, *, factory: Callable[..., Any] = _default_factory) -> None:
        self._factory = factory
        self._requires_local_assets = factory is _default_factory

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
        language = configuration.languages[0]
        options.setdefault("lang", LANGUAGE_MODEL_NAMES[language])
        options.setdefault("use_doc_orientation_classify", False)
        options.setdefault("use_doc_unwarping", False)
        options.setdefault("use_textline_orientation", False)
        options.setdefault("enable_mkldnn", False)
        assets = _local_assets(language) if self._requires_local_assets else None
        if assets is not None:
            options.setdefault("ocr_version", assets.ocr_version)
            options.setdefault("text_detection_model_name", assets.detection_name)
            options.setdefault("text_detection_model_dir", str(assets.detection_dir))
            options.setdefault("text_recognition_model_name", assets.recognition_name)
            options.setdefault("text_recognition_model_dir", str(assets.recognition_dir))
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
            raise OcrEngineError(
                "PaddleOCR execution failed.", code="PADDLEOCR_FAILED", retryable=True
            ) from exc
        duration_ms = round((perf_counter() - started) * 1000)
        metadata = EngineMetadata(
            engine="paddleocr",
            engine_version=_package_version("paddleocr"),
            model_versions={
                "paddlepaddle": _package_version("paddlepaddle"),
                "ocr": str(options.get("ocr_version", "PP-OCRv5")),
                "language": str(options["lang"]),
                **(assets.versions if assets is not None else {}),
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
