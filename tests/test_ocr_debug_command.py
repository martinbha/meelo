from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from PIL import Image

from apps.ocr.contracts import BoundingBox, EngineMetadata, OcrRunResult, OcrToken


@override_settings(DEBUG=False)
def test_debug_command_is_disabled_in_production(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="disabled"):
        call_command("render_ocr_debug", tmp_path / "input.png", tmp_path / "output.png")


@override_settings(DEBUG=True)
def test_debug_command_rejects_repository_output(tmp_path: Path, settings: Any) -> None:
    image = tmp_path / "input.png"
    Image.new("RGB", (10, 10), "white").save(image)
    output = Path(settings.BASE_DIR) / "debug-output.png"

    with pytest.raises(CommandError, match="outside the repository"):
        call_command("render_ocr_debug", image, output)


@override_settings(DEBUG=True)
def test_debug_command_renders_without_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "input.png"
    output = tmp_path / "outside" / "overlay.png"
    Image.new("RGB", (20, 20), "white").save(image)

    class Engine:
        def __init__(self, name: str) -> None:
            self.engine_name = name

        def run(self, image_path: Path, configuration: Any) -> OcrRunResult:
            del image_path
            return OcrRunResult(
                (OcrToken("token", 0.9, BoundingBox(2, 2, 10, 10)),),
                EngineMetadata(self.engine_name, "1"),
                configuration,
                1,
            )

    monkeypatch.setattr(
        "apps.ocr.management.commands.render_ocr_debug.PaddleOcrEngine",
        lambda: Engine("paddleocr"),
    )
    monkeypatch.setattr(
        "apps.ocr.management.commands.render_ocr_debug.TesseractOcrEngine",
        lambda: Engine("tesseract"),
    )

    call_command("render_ocr_debug", image, output)

    assert output.is_file()
    with Image.open(output) as overlay:
        assert overlay.size == (40, 40)
