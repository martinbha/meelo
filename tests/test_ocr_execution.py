import time
from pathlib import Path

import pytest

from apps.ocr.contracts import (
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrEngineError,
    OcrRunResult,
)
from apps.ocr.execution import ClassifiedOcrError, run_engine_bounded


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
