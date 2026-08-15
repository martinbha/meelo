import time
from pathlib import Path

import pytest

from apps.ocr.contracts import (
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
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
