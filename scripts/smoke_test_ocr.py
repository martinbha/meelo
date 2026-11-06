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
from apps.ocr.paddle import (
    DEFAULT_OCR_CPU_THREADS,
    DEFAULT_TEXT_DET_LIMIT_SIDE_LEN,
    DEFAULT_TEXT_DET_LIMIT_TYPE,
    PaddleOcrEngine,
)
from apps.ocr.tesseract import TesseractOcrEngine
from apps.ocr.tesseract_psm import default_psm

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


def create_synthetic_image(path: Path, *, size: tuple[int, int]) -> None:
    """Create a transaction-row probe at one input geometry."""

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    row_font = ImageFont.load_default(size=112)
    left, top, _, _ = draw.textbbox((0, 0), "CAFE " + SMOKE_TEXT, font=row_font, stroke_width=2)
    draw.text(
        (80 - left, image.height // 2 - top),
        "CAFE " + SMOKE_TEXT,
        font=row_font,
        fill="black",
        stroke_width=2,
    )
    if image.height > image.width:
        header_font = ImageFont.load_default(size=56)
        draw.text((80, 100), "TRANSACTION HISTORY", font=header_font, fill="black")
    image.save(path, format="PNG")


def verify_engine(
    name: str, engine: OcrEngine, image_path: Path, languages: tuple[str, ...]
) -> None:
    try:
        options: dict[str, str | int]
        if name == "paddleocr":
            options = {
                "device": "cpu",
                "cpu_threads": DEFAULT_OCR_CPU_THREADS,
                "text_det_limit_side_len": DEFAULT_TEXT_DET_LIMIT_SIDE_LEN,
                "text_det_limit_type": DEFAULT_TEXT_DET_LIMIT_TYPE,
            }
        else:
            options = {"psm": default_psm("bank_transaction_list")}
        result = run_engine_bounded(
            engine,
            image_path,
            OcrConfiguration(languages, options),
            limits=RESOURCE_LIMITS,
        )
    except ClassifiedOcrError as error:
        raise RuntimeError(f"{name} synthetic smoke failed with {error.code}") from error
    recognized = "".join(
        character for token in result.tokens for character in token.text if character.isalnum()
    )
    if SMOKE_TEXT not in recognized:
        raise RuntimeError(
            f"{name} did not recognize the synthetic numeric marker in {image_path.name}."
        )
    print(
        f"{name} smoke passed; image={image_path.stem}; "
        f"tokens={len(result.tokens)}; duration_ms={result.duration_ms}"
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="meelo-ocr-smoke-") as temporary:
        root = Path(temporary)
        image_paths = (
            root / "synthetic-wide-12345.png",
            root / "synthetic-portrait-12345.png",
        )
        create_synthetic_image(image_paths[0], size=(900, 240))
        create_synthetic_image(image_paths[1], size=(1080, 1920))
        engines: tuple[tuple[str, OcrEngine, tuple[str, ...]], ...] = (
            ("tesseract", TesseractOcrEngine(), ("ko", "en")),
            ("paddleocr", SmokePaddleOcrEngine(), ("ko",)),
        )
        for name, engine, languages in engines:
            for image_path in image_paths:
                verify_engine(name, engine, image_path, languages)


if __name__ == "__main__":
    main()
