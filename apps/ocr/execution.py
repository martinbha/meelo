from __future__ import annotations

import multiprocessing
import os
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrEngineError,
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


@dataclass(frozen=True, slots=True)
class OcrResourceLimits:
    timeout_seconds: float = 120.0
    max_threads: int = 2
    memory_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_threads <= 0 or self.memory_bytes <= 0:
            raise ValueError("OCR resource limits must be positive.")


def _apply_resource_limits(limits: OcrResourceLimits) -> None:
    thread_count = str(limits.max_threads)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = thread_count
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    except (ImportError, OSError, ValueError):
        # Windows and constrained runtimes rely on their container memory cap.
        pass


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
        configuration=OcrConfiguration(tuple(configuration["languages"]), configuration["options"]),
        duration_ms=payload["duration_ms"],
        raw_output=payload["raw_output"],
    )


def _failure_details(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, MemoryError):
        return "OCR_RESOURCE_EXHAUSTED", True
    if isinstance(exc, OcrEngineError):
        return exc.code, exc.retryable
    if isinstance(exc, UnsupportedLanguageError | OcrConfigurationError):
        return "OCR_CONFIGURATION_INVALID", False
    if isinstance(exc, OcrError):
        return "OCR_ENGINE_FAILED", True
    return "OCR_ENGINE_CRASHED", True


def _run_child(
    output: Any,
    engine: OcrEngine,
    image_path: Path,
    languages: tuple[str, ...],
    options: dict[str, Any],
    limits: OcrResourceLimits,
) -> None:
    try:
        _apply_resource_limits(limits)
        configuration = OcrConfiguration(languages, options)
        output.put(("success", _result_payload(engine.run(image_path, configuration))))
    except Exception as exc:
        output.put(("failure", _failure_details(exc)))


def run_engine_bounded(
    engine: OcrEngine,
    image_path: Path,
    configuration: OcrConfiguration,
    *,
    timeout_seconds: float | None = None,
    limits: OcrResourceLimits | None = None,
) -> OcrRunResult:
    limits = limits or OcrResourceLimits(
        timeout_seconds=timeout_seconds if timeout_seconds is not None else 120.0
    )
    if timeout_seconds is None:
        timeout_seconds = limits.timeout_seconds
    if timeout_seconds <= 0:
        raise ValueError("OCR timeout must be positive.")
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    prepare = getattr(engine, "prepare", None) if method == "fork" else None
    try:
        if callable(prepare):
            prepare(configuration)
    except Exception as exc:
        code, retryable = _failure_details(exc)
        raise ClassifiedOcrError(code=code, retryable=retryable) from exc
    context: Any = multiprocessing.get_context(method)
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_run_child,
        args=(
            output,
            engine,
            image_path,
            tuple(configuration.languages),
            dict(configuration.options),
            limits,
        ),
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
        reset = getattr(engine, "reset", None)
        if callable(reset):
            reset(configuration)
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
        reset = getattr(engine, "reset", None)
        if callable(reset):
            reset(configuration)
        code, retryable = payload
        raise ClassifiedOcrError(code=code, retryable=retryable)
    return _result_from_payload(payload)
