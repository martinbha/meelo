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

        cgroup_limit = _cgroup_memory_limit_bytes()
        baseline_data = _current_vm_data_bytes()
        if (
            cgroup_limit is not None
            and cgroup_limit <= limits.memory_bytes
            and baseline_data is not None
            and hasattr(resource, "RLIMIT_DATA")
        ):
            # Paddle initializes shared libraries and model state before this
            # child starts. Allow the configured additional data allocation
            # above that measured baseline; the cgroup remains the hard total
            # memory limit. This does not make RLIMIT_DATA a total RSS limit.
            data_ceiling = baseline_data + limits.memory_bytes
            try:
                resource.setrlimit(resource.RLIMIT_DATA, (data_ceiling, data_ceiling))
                return
            except (OSError, ValueError):
                # If the data limit cannot be installed, retain the absolute
                # address-space ceiling below.
                pass
        # Without a known cgroup cap no larger than this worker's budget, or a
        # trusted VmData baseline, keep the conservative absolute ceiling.
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    except (ImportError, OSError, ValueError):
        # Windows and constrained runtimes rely on their container memory cap.
        pass


def _cgroup_memory_limit_bytes() -> int | None:
    """Return a detected finite cgroup limit, or ``None`` when unknown."""

    cgroup_limits = (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    )
    limits: list[int] = []
    for path in cgroup_limits:
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value == "max":
            continue
        try:
            limit = int(value)
        except ValueError:
            continue
        # cgroup v1 represents unlimited memory with a very large sentinel.
        if 0 < limit < 1 << 60:
            limits.append(limit)
    return min(limits) if limits else None


def _current_vm_data_bytes() -> int | None:
    """Read Linux VmData as the pre-inference data-segment baseline."""

    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("VmData:"):
            continue
        fields = line.partition(":")[2].split()
        if len(fields) != 2 or fields[1] != "kB":
            return None
        try:
            kilobytes = int(fields[0])
        except ValueError:
            return None
        return kilobytes * 1024 if kilobytes > 0 else None
    return None


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
