from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from PIL import Image

from apps.ocr.contracts import OcrConfiguration, OcrConfigurationError
from apps.ocr.tesseract import TesseractOcrEngine, inspect_tesseract_installation


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
        runner=runner,
        languages=lambda: ["eng", "kor"],
        version=lambda: "5.5.1",
        language_versions=lambda packs: {pack: f"digest-{pack}" for pack in packs},
    )
    result = engine.run(image_path, OcrConfiguration(("ko", "en"), {"psm": 11}))

    assert result.text == "결제"
    assert result.tokens[0].confidence == 0.875
    assert result.tokens[0].bounding_box.right == 30
    assert result.tokens[0].hierarchy == (1, 2, 1, 3, 4)
    assert result.metadata.engine_version == "5.5.1"
    assert result.metadata.model_versions == {"kor": "digest-kor", "eng": "digest-eng"}
    assert calls == [{"lang": "kor+eng", "config": "--psm 11"}]


def test_tesseract_adapter_reports_missing_language_pack(tmp_path: Path) -> None:
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    engine = TesseractOcrEngine(
        languages=lambda: ["eng"],
        version=lambda: "5",
        language_versions=lambda packs: {},
    )

    with pytest.raises(OcrConfigurationError, match="kor"):
        engine.run(image_path, OcrConfiguration(("ko",)))


def test_tesseract_adapter_rejects_unsupported_psm(tmp_path: Path) -> None:
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    engine = TesseractOcrEngine(
        languages=lambda: ["eng"],
        version=lambda: "5",
        language_versions=lambda packs: {},
    )

    with pytest.raises(OcrConfigurationError, match="PSM"):
        engine.run(image_path, OcrConfiguration(("en",), {"psm": 99}))


def test_tesseract_adapter_records_language_data_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"english-v1")
    (tessdata / "kor.traineddata").write_bytes(b"korean-v2")
    monkeypatch.setenv("TESSDATA_PREFIX", str(tessdata))
    image_path = tmp_path / "fixture.png"
    make_image(image_path)
    engine = TesseractOcrEngine(
        runner=lambda image, **kwargs: {"text": []},
        languages=lambda: ["eng", "kor"],
        version=lambda: "5.5.1",
    )

    result = engine.run(image_path, OcrConfiguration(("ko", "en")))

    assert result.metadata.engine_version == "5.5.1"
    assert result.metadata.model_versions == {
        "language_kor": "sha256:0874206a177d83d573b502df5f302ab4db614c648077cc584a5df9f07e726d51",
        "language_eng": "sha256:39e29f134404576294bf69a26051e8a0952e489f8370a3a71bf6b4d52f5dca9d",
    }


def test_installation_inspection_reports_missing_required_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.ocr.tesseract._default_languages", lambda: ["eng"])

    with pytest.raises(OcrConfigurationError, match="kor"):
        inspect_tesseract_installation()


@override_settings(OCR_VERIFY_TESSERACT_INSTALLATION=True)
def test_application_startup_rejects_invalid_tesseract_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.ocr.apps import OcrConfig

    def invalid_installation() -> None:
        raise OcrConfigurationError("Missing required Tesseract language pack(s): kor.")

    monkeypatch.setattr("apps.ocr.apps.inspect_tesseract_installation", invalid_installation)

    with pytest.raises(ImproperlyConfigured, match="kor"):
        OcrConfig.ready(OcrConfig("apps.ocr", __import__("apps.ocr")))
