from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.ocr.contracts import OcrConfiguration
from apps.ocr.fixture_harness import render_debug_overlay
from apps.ocr.matching import TokenCandidate, match_engine_tokens
from apps.ocr.paddle import PaddleOcrEngine
from apps.ocr.preprocessing import PreprocessingSettings, preprocess_image, select_variant
from apps.ocr.tesseract import TesseractOcrEngine
from apps.ocr.tesseract_psm import default_psm


class Command(BaseCommand):
    help = "Render a development-only OCR token and disagreement overlay."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("image", type=Path)
        parser.add_argument("output", type=Path)
        parser.add_argument("--source-type", default="unknown")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        if not settings.DEBUG:
            raise CommandError("OCR debug overlays are disabled outside development.")
        image_path = options["image"].expanduser().resolve()
        output_path = options["output"].expanduser().resolve()
        repository = Path(settings.BASE_DIR).resolve()
        if output_path.is_relative_to(repository):
            raise CommandError("OCR debug output must be outside the repository.")
        if not image_path.is_file():
            raise CommandError("The fixture image does not exist.")

        source_type = str(options["source_type"])
        plans = (
            (PaddleOcrEngine(), OcrConfiguration(("ko",), {"device": "cpu"})),
            (
                TesseractOcrEngine(),
                OcrConfiguration(("ko", "en"), {"psm": default_psm(source_type)}),
            ),
        )
        candidates: list[tuple[TokenCandidate, ...]] = []
        with (
            tempfile.TemporaryDirectory(prefix="ocr-debug-") as working,
            preprocess_image(
                image_path, Path(working), PreprocessingSettings(scale=2.0)
            ) as prepared,
        ):
            for engine, configuration in plans:
                variant = select_variant(engine=engine.engine_name, source_type=source_type)
                result = engine.run(prepared.variant(variant).path, configuration)
                candidates.append(
                    tuple(
                        TokenCandidate.from_token(engine.engine_name, token)
                        for token in result.tokens
                    )
                )
            groups = match_engine_tokens(candidates[0], candidates[1])
            render_debug_overlay(prepared.variant("normalized").path, groups, output_path)
        self.stdout.write(str(output_path))
