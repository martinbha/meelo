from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.ocr.contracts import OcrConfiguration, OcrConfigurationError
from apps.ocr.tesseract import TesseractOcrEngine


def make_image(path: Path) -> None:
    Image.new("RGB", (64, 32), "white").save(path)


def test_tesseract_adapter_preserves_tsv_hierarchy_and_bounds(tmp_path: Path) -> None:
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    calls: list[dict[str, Any]] = []

    def runner(image: Image.Image, **kwargs: Any) -> dict[str, list[Any]]:
        calls.append(kwargs)
        assert image.size == (64, 32)
        return {
            "text": ["", "결제"],
            "conf": ["-1", "87.5"],
            "left": [0, 10],
            "top": [0, 5],
            "width": [64, 20],
            "height": [32, 8],
            "page_num": [1, 1],
            "block_num": [0, 2],
            "par_num": [0, 1],
            "line_num": [0, 3],
            "word_num": [0, 4],
        }

    engine = TesseractOcrEngine(
        runner=runner, languages=lambda: ["eng", "kor"], version=lambda: "5.5.1"
    )
    result = engine.run(image_path, OcrConfiguration(("ko", "en"), {"psm": 11}))

    assert result.text == "결제"
    assert result.tokens[0].confidence == 0.875
    assert result.tokens[0].bounding_box.right == 30
    assert result.tokens[0].hierarchy == (1, 2, 1, 3, 4)
    assert result.metadata.engine_version == "5.5.1"
    assert calls == [{"lang": "kor+eng", "config": "--psm 11"}]


def test_tesseract_adapter_reports_missing_language_pack(tmp_path: Path) -> None:
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    engine = TesseractOcrEngine(languages=lambda: ["eng"], version=lambda: "5")

    with pytest.raises(OcrConfigurationError, match="kor"):
        engine.run(image_path, OcrConfiguration(("ko",)))


def test_tesseract_adapter_rejects_unsupported_psm(tmp_path: Path) -> None:
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    engine = TesseractOcrEngine(languages=lambda: ["eng"], version=lambda: "5")

    with pytest.raises(OcrConfigurationError, match="PSM"):
        engine.run(image_path, OcrConfiguration(("en",), {"psm": 99}))
