import os
import resource
import time
from pathlib import Path
from typing import Any

import pytest

from apps.ocr.contracts import (
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrEngineError,
    OcrRunResult,
)
from apps.ocr.execution import (
    ClassifiedOcrError,
    OcrResourceLimits,
    _apply_resource_limits,
    _cgroup_memory_limit_bytes,
    _current_vm_data_bytes,
    _failure_details,
    run_engine_bounded,
)


class SlowEngine(OcrEngine):
    @property
    def metadata(self) -> EngineMetadata:
        return EngineMetadata("slow", "1")

    @property
    def supported_languages(self) -> frozenset[str]:
        return frozenset({"en"})

    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        time.sleep(5)
        return OcrRunResult((), self.metadata, configuration, 5000)


class InvalidEngine(SlowEngine):
    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        raise OcrConfigurationError("missing local language data")


class LargeResultEngine(SlowEngine):
    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        return OcrRunResult(
            (),
            self.metadata,
            configuration,
            10,
            raw_output="x" * (2 * 1024 * 1024),
        )


class NamedFailureEngine(SlowEngine):
    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        raise OcrEngineError("missing assets", code="PADDLEOCR_FAILED", retryable=False)


class SpawnEngine(LargeResultEngine):
    prepared = False

    def prepare(self, configuration: OcrConfiguration) -> None:
        del configuration
        self.prepared = True


def test_bounded_execution_terminates_hung_engine() -> None:
    started = time.monotonic()
    with pytest.raises(ClassifiedOcrError) as failure:
        run_engine_bounded(
            SlowEngine(),
            Path("fixture.png"),
            OcrConfiguration(("en",)),
            timeout_seconds=0.05,
        )

    assert time.monotonic() - started < 1
    assert failure.value.code == "OCR_ENGINE_TIMEOUT"
    assert failure.value.retryable is True


def test_bounded_execution_marks_configuration_failure_permanent() -> None:
    with pytest.raises(ClassifiedOcrError) as failure:
        run_engine_bounded(
            InvalidEngine(),
            Path("fixture.png"),
            OcrConfiguration(("en",)),
            timeout_seconds=1,
        )

    assert failure.value.code == "OCR_CONFIGURATION_INVALID"
    assert failure.value.retryable is False


def test_bounded_execution_preserves_engine_failure_code() -> None:
    with pytest.raises(ClassifiedOcrError) as failure:
        run_engine_bounded(
            NamedFailureEngine(),
            Path("fixture.png"),
            OcrConfiguration(("en",)),
            timeout_seconds=1,
        )

    assert failure.value.code == "PADDLEOCR_FAILED"
    assert failure.value.retryable is False


def test_bounded_execution_drains_large_result_before_joining_child() -> None:
    result = run_engine_bounded(
        LargeResultEngine(),
        Path("fixture.png"),
        OcrConfiguration(("en",)),
        timeout_seconds=2,
    )

    assert len(result.raw_output) == 2 * 1024 * 1024


def test_bounded_execution_prepares_and_resets_warm_engines() -> None:
    events: list[str] = []

    class WarmInvalidEngine(InvalidEngine):
        def prepare(self, configuration: OcrConfiguration) -> None:
            del configuration
            events.append("prepared")

        def reset(self, configuration: OcrConfiguration) -> None:
            del configuration
            events.append("reset")

    with pytest.raises(ClassifiedOcrError):
        run_engine_bounded(
            WarmInvalidEngine(),
            Path("fixture.png"),
            OcrConfiguration(("en",)),
            timeout_seconds=1,
        )

    assert events == ["prepared", "reset"]


def test_spawn_execution_does_not_prepare_an_unpicklable_parent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.ocr.execution.multiprocessing.get_all_start_methods", lambda: ["spawn"]
    )
    engine = SpawnEngine()
    result = run_engine_bounded(
        engine, Path("fixture.png"), OcrConfiguration(("en",)), timeout_seconds=2
    )

    assert result.duration_ms == 10
    assert engine.prepared is False


def test_resource_limits_validate_and_classify_memory_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        OcrResourceLimits(max_threads=0)
    assert _failure_details(MemoryError()) == ("OCR_RESOURCE_EXHAUSTED", True)

    applied: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr("resource.setrlimit", lambda kind, value: applied.append((kind, value)))
    monkeypatch.setattr("apps.ocr.execution._cgroup_memory_limit_bytes", lambda: None)
    monkeypatch.setattr("apps.ocr.execution._current_vm_data_bytes", lambda: 4096)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        monkeypatch.setenv(name, "original")
    limits = OcrResourceLimits(timeout_seconds=3, max_threads=1, memory_bytes=1024)
    _apply_resource_limits(limits)

    assert os.environ["OMP_NUM_THREADS"] == "1"
    assert applied[0][0] == resource.RLIMIT_AS
    assert applied[0][1] == (1024, 1024)


@pytest.mark.parametrize(
    ("cgroup_limit", "baseline", "expected_resource", "expected_value"),
    [
        (2048, 4096, resource.RLIMIT_DATA, 4096 + 2048),
        (2049, 4096, resource.RLIMIT_AS, 2048),
        (None, 4096, resource.RLIMIT_AS, 2048),
        (2048, None, resource.RLIMIT_AS, 2048),
    ],
)
def test_data_allowance_requires_a_trusted_baseline_and_cgroup_within_budget(
    monkeypatch: pytest.MonkeyPatch,
    cgroup_limit: int | None,
    baseline: int | None,
    expected_resource: int,
    expected_value: int,
) -> None:
    applied: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr("resource.setrlimit", lambda kind, value: applied.append((kind, value)))
    monkeypatch.setattr("apps.ocr.execution._cgroup_memory_limit_bytes", lambda: cgroup_limit)
    monkeypatch.setattr("apps.ocr.execution._current_vm_data_bytes", lambda: baseline)

    _apply_resource_limits(OcrResourceLimits(memory_bytes=2048))

    assert applied == [(expected_resource, (expected_value, expected_value))]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"/sys/fs/cgroup/memory.max": str(2 * 1024**3)}, 2 * 1024**3),
        ({"/sys/fs/cgroup/memory.max": "max"}, None),
        ({"/sys/fs/cgroup/memory.max": "invalid"}, None),
        ({}, None),
        (
            {
                "/sys/fs/cgroup/memory.max": "max",
                "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(1 << 63),
            },
            None,
        ),
    ],
)
def test_cgroup_memory_limit_detection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    expected: int | None,
) -> None:
    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        try:
            return values[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    monkeypatch.setattr(Path, "read_text", read_text)

    assert _cgroup_memory_limit_bytes() == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Name: worker\nVmData: 1234 kB\n", 1234 * 1024),
        ("Name: worker\nVmData: invalid kB\n", None),
        ("Name: worker\n", None),
    ],
)
def test_vm_data_baseline_parsing_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: int | None,
) -> None:
    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        if path == Path("/proc/self/status"):
            return status
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert _current_vm_data_bytes() == expected


def test_data_allowance_refuses_oversized_allocation_under_kernel_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(resource, "RLIMIT_DATA"):
        pytest.skip("RLIMIT_DATA is unavailable")
    if _current_vm_data_bytes() is None:
        pytest.skip("A Linux /proc VmData baseline is unavailable")

    budget = 8 * 1024 * 1024
    monkeypatch.setattr("apps.ocr.execution._cgroup_memory_limit_bytes", lambda: budget)

    class AllocationEngine(SlowEngine):
        def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
            del image_path
            bytearray(32 * 1024 * 1024)
            return OcrRunResult((), self.metadata, configuration, 10)

    with pytest.raises(ClassifiedOcrError) as failure:
        run_engine_bounded(
            AllocationEngine(),
            Path("fixture.png"),
            OcrConfiguration(("en",)),
            limits=OcrResourceLimits(timeout_seconds=5, memory_bytes=budget),
        )

    assert failure.value.code == "OCR_RESOURCE_EXHAUSTED"
