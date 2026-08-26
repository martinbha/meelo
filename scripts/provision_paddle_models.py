"""Provision pinned PaddleOCR models and record their content digests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

OCR_VERSION = "PP-OCRv5"
DETECTION_MODEL = "PP-OCRv5_server_det"
RECOGNITION_MODELS = {
    "en": "en_PP-OCRv5_mobile_rec",
    "ko": "korean_PP-OCRv5_mobile_rec",
}


def _digest(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and ".cache" not in path.parts
    )
    if not files:
        raise RuntimeError(f"PaddleOCR model directory is empty: {directory}")
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def provision(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    from paddleocr import PaddleOCR  # type: ignore[import-untyped]

    for language in RECOGNITION_MODELS:
        PaddleOCR(
            lang="korean" if language == "ko" else "en",
            ocr_version=OCR_VERSION,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
        )

    models = root / "official_models"
    recognition = {
        language: {
            "name": name,
            "directory": str(Path("official_models") / name),
            "sha256": _digest(models / name),
        }
        for language, name in RECOGNITION_MODELS.items()
    }
    manifest = {
        "format": 1,
        "ocr_version": OCR_VERSION,
        "detection": {
            "name": DETECTION_MODEL,
            "directory": str(Path("official_models") / DETECTION_MODEL),
            "sha256": _digest(models / DETECTION_MODEL),
        },
        "recognition": recognition,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    provision(args.root.resolve())


if __name__ == "__main__":
    main()
