"""Run both packaged OCR engines on a generated image without network access."""

from __future__ import annotations

import os
import tempfile
import traceback
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ["PADDLE_PDX_CACHE_HOME"] = "/tmp/paddlex-cache"

from apps.ocr.contracts import OcrConfiguration, OcrEngine, OcrRunResult
from apps.ocr.execution import ClassifiedOcrError, OcrResourceLimits, run_engine_bounded
from apps.ocr.paddle import PaddleOcrEngine
from apps.ocr.tesseract import TesseractOcrEngine

SMOKE_TEXT = "12345"
RESOURCE_LIMITS = OcrResourceLimits(
    timeout_seconds=90,
    max_threads=2,
    memory_bytes=2 * 1024 * 1024 * 1024,
)


class SmokePaddleOcrEngine(PaddleOcrEngine):
    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        try:
            return super().run(image_path, configuration)
        except Exception:
            # This process handles only the synthetic marker created below.
            # Keep library diagnostics here, never in the financial pipeline.
            traceback.print_exc()
            raise


def create_synthetic_image(path: Path) -> None:
    """Create a high-contrast image using Pillow's bundled default font."""

    image = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=112)
    left, top, right, bottom = draw.textbbox((0, 0), SMOKE_TEXT, font=font, stroke_width=2)
    origin = (
        (image.width - (right - left)) // 2 - left,
        (image.height - (bottom - top)) // 2 - top,
    )
    draw.text(origin, SMOKE_TEXT, font=font, fill="black", stroke_width=2)
    image.save(path, format="PNG")


def verify_engine(
    name: str, engine: OcrEngine, image_path: Path, languages: tuple[str, ...]
) -> None:
    try:
        result = run_engine_bounded(
            engine,
            image_path,
            OcrConfiguration(languages, {"device": "cpu"} if name == "paddleocr" else {"psm": 7}),
            limits=RESOURCE_LIMITS,
        )
    except ClassifiedOcrError as error:
        raise RuntimeError(f"{name} synthetic smoke failed with {error.code}") from error
    recognized = "".join(
        character for token in result.tokens for character in token.text if character.isalnum()
    )
    if SMOKE_TEXT not in recognized:
        raise RuntimeError(f"{name} did not recognize the synthetic numeric marker.")
    print(f"{name} smoke passed; tokens={len(result.tokens)}; duration_ms={result.duration_ms}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="meelo-ocr-smoke-") as temporary:
        image_path = Path(temporary) / "synthetic-12345.png"
        create_synthetic_image(image_path)
        verify_engine("tesseract", TesseractOcrEngine(), image_path, ("en",))
        verify_engine("paddleocr", SmokePaddleOcrEngine(), image_path, ("en",))


if __name__ == "__main__":
    main()
