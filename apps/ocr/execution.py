from __future__ import annotations

import multiprocessing
import queue
from pathlib import Path
from typing import Any

from .contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrError,
    OcrRunResult,
    OcrToken,
    UnsupportedLanguageError,
)


class ClassifiedOcrError(OcrError):
    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__("The local OCR engine did not complete.")
        self.code = code
        self.retryable = retryable


def _result_payload(result: OcrRunResult) -> dict[str, Any]:
    return {
        "tokens": [
            {
                "text": token.text,
                "confidence": token.confidence,
                "box": (
                    token.bounding_box.left,
                    token.bounding_box.top,
                    token.bounding_box.right,
                    token.bounding_box.bottom,
                ),
                "hierarchy": token.hierarchy,
            }
            for token in result.tokens
        ],
        "metadata": {
            "engine": result.metadata.engine,
            "engine_version": result.metadata.engine_version,
            "model_versions": dict(result.metadata.model_versions),
        },
        "configuration": {
            "languages": result.configuration.languages,
            "options": dict(result.configuration.options),
        },
        "duration_ms": result.duration_ms,
        "raw_output": result.raw_output,
    }


def _result_from_payload(payload: dict[str, Any]) -> OcrRunResult:
    metadata = payload["metadata"]
    configuration = payload["configuration"]
    return OcrRunResult(
        tokens=tuple(
            OcrToken(
                token["text"],
                token["confidence"],
                BoundingBox(*token["box"]),
                tuple(token["hierarchy"]),
            )
            for token in payload["tokens"]
        ),
        metadata=EngineMetadata(
            metadata["engine"], metadata["engine_version"], metadata["model_versions"]
        ),
        configuration=OcrConfiguration(
            tuple(configuration["languages"]), configuration["options"]
        ),
        duration_ms=payload["duration_ms"],
        raw_output=payload["raw_output"],
    )


def _failure_details(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, UnsupportedLanguageError | OcrConfigurationError):
        return "OCR_CONFIGURATION_INVALID", False
    if isinstance(exc, OcrError):
        return "OCR_ENGINE_FAILED", True
    return "OCR_ENGINE_CRASHED", True


def _run_child(
    output: Any,
    engine: OcrEngine,
    image_path: Path,
    configuration: OcrConfiguration,
) -> None:
    try:
        output.put(("success", _result_payload(engine.run(image_path, configuration))))
    except Exception as exc:
        output.put(("failure", _failure_details(exc)))


def run_engine_bounded(
    engine: OcrEngine,
    image_path: Path,
    configuration: OcrConfiguration,
    *,
    timeout_seconds: float,
) -> OcrRunResult:
    if timeout_seconds <= 0:
        raise ValueError("OCR timeout must be positive.")
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    context: Any = multiprocessing.get_context(method)
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_run_child,
        args=(output, engine, image_path, configuration),
    )
    process.start()
    try:
        status, payload = output.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        timed_out = process.is_alive()
        if timed_out:
            process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join()
        code = "OCR_ENGINE_TIMEOUT" if timed_out else "OCR_ENGINE_CRASHED"
        raise ClassifiedOcrError(code=code, retryable=True) from exc
    finally:
        output.close()
        output.join_thread()
    process.join(1)
    if process.is_alive():
        process.terminate()
        process.join()
    if status == "failure":
        code, retryable = payload
        raise ClassifiedOcrError(code=code, retryable=retryable)
    return _result_from_payload(payload)
