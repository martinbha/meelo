from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from apps.ocr.contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrEngine,
    OcrRunResult,
    OcrToken,
    UnsupportedLanguageError,
)


class StubEngine(OcrEngine):
    @property
    def metadata(self) -> EngineMetadata:
        return EngineMetadata("stub", "1.0", {"korean": "2026.08"})

    @property
    def supported_languages(self) -> frozenset[str]:
        return frozenset({"en", "ko"})

    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        self.validate_configuration(configuration)
        return OcrRunResult(
            tokens=(OcrToken("결제", 0.91, BoundingBox(1, 2, 20, 12)),),
            metadata=self.metadata,
            configuration=configuration,
            duration_ms=4,
        )


def test_adapter_contract_returns_immutable_normalized_tokens(tmp_path: Path) -> None:
    configuration = OcrConfiguration(("KO", "en", "ko"), {"device": "cpu"})
    result = StubEngine().run(tmp_path / "fixture.png", configuration)

    assert configuration.languages == ("ko", "en")
    assert result.text == "결제"
    assert result.tokens[0].bounding_box == BoundingBox(1, 2, 20, 12)
    assert result.metadata.model_versions["korean"] == "2026.08"
    with pytest.raises(FrozenInstanceError):
        result.duration_ms = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        configuration.options["device"] = "gpu"  # type: ignore[index]


def test_adapter_contract_rejects_unsupported_languages() -> None:
    with pytest.raises(UnsupportedLanguageError, match="ja"):
        StubEngine().run(Path("fixture.png"), OcrConfiguration(("ja",)))


@pytest.mark.parametrize("box", [BoundingBox(0, 0, 0, 0), BoundingBox(1, 2, 3, 4)])
def test_bounding_box_accepts_valid_edges(box: BoundingBox) -> None:
    assert box.right >= box.left
    assert box.bottom >= box.top


def test_contract_rejects_invalid_confidence_and_geometry() -> None:
    with pytest.raises(ValueError, match="confidence"):
        OcrToken("bad", 1.1, BoundingBox(0, 0, 1, 1))
    with pytest.raises(ValueError, match="out of order"):
        BoundingBox(2, 0, 1, 1)
