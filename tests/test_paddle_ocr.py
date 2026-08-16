import json
from pathlib import Path
from typing import Any

import pytest

from apps.ocr.contracts import OcrConfiguration, OcrConfigurationError
from apps.ocr.paddle import PaddleOcrEngine


class FakePaddle:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[tuple[str, bool]] = []

    def ocr(self, path: str, *, cls: bool) -> Any:
        self.calls.append((path, cls))
        return self.output


class FakePaddleV3:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[str] = []

    def predict(self, path: str) -> Any:
        self.calls.append(path)
        return self.output


def test_paddle_adapter_normalizes_korean_tokens_without_network(tmp_path: Path) -> None:
    image = tmp_path / "sanitized.png"
    image.write_bytes(b"local fixture")
    paddle = FakePaddle([[[[[10, 20], [50, 20], [50, 35], [10, 35]], ["결제", 0.975]]]])
    factory_options: dict[str, Any] = {}

    def factory(**options: Any) -> FakePaddle:
        factory_options.update(options)
        return paddle

    result = PaddleOcrEngine(factory=factory).run(image, OcrConfiguration(("ko",)))

    assert result.text == "결제"
    assert result.tokens[0].confidence == 0.975
    assert result.tokens[0].bounding_box.right == 50
    assert factory_options == {
        "lang": "korean",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    }
    assert paddle.calls == [(str(image), False)]
    assert "결제" in result.raw_output


def test_paddle_adapter_normalizes_current_mapping_results(tmp_path: Path) -> None:
    image = tmp_path / "sanitized.png"
    image.touch()
    paddle = FakePaddleV3(
        [
            {
                "rec_texts": ["승인", "10,000원"],
                "rec_scores": [0.98, 0.93],
                "rec_polys": [
                    [[1, 2], [20, 2], [20, 8], [1, 8]],
                    [[1, 10], [40, 10], [40, 18], [1, 18]],
                ],
            }
        ]
    )

    result = PaddleOcrEngine(factory=lambda **options: paddle).run(
        image, OcrConfiguration(("ko",), {"ocr_version": "PP-OCRv5"})
    )

    assert result.text == "승인 10,000원"
    assert paddle.calls == [str(image)]
    assert result.metadata.model_versions["ocr"] == "PP-OCRv5"
    assert result.metadata.model_versions["language"] == "korean"
    assert json.loads(result.raw_output)[0]["rec_texts"] == ["승인", "10,000원"]


def test_paddle_adapter_records_explicit_model_configuration(tmp_path: Path) -> None:
    image = tmp_path / "fixture.png"
    image.touch()
    captured: dict[str, Any] = {}

    def factory(**options: Any) -> FakePaddle:
        captured.update(options)
        return FakePaddle([])

    configuration = OcrConfiguration(("en",), {"device": "cpu", "text_det_limit_side_len": 960})
    result = PaddleOcrEngine(factory=factory).run(image, configuration)

    assert result.configuration == configuration
    assert captured["device"] == "cpu"
    assert captured["text_det_limit_side_len"] == 960
    assert result.metadata.engine == "paddleocr"
    assert "paddlepaddle" in result.metadata.model_versions


def test_paddle_adapter_rejects_missing_input_and_multi_model_runs(tmp_path: Path) -> None:
    adapter = PaddleOcrEngine(factory=lambda **options: FakePaddle([]))
    with pytest.raises(OcrConfigurationError, match="does not exist"):
        adapter.run(tmp_path / "missing.png", OcrConfiguration(("ko",)))

    image = tmp_path / "fixture.png"
    image.touch()
    with pytest.raises(OcrConfigurationError, match="exactly one"):
        adapter.run(image, OcrConfiguration(("ko", "en")))
