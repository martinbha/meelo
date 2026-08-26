from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from PIL import Image

from .contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrRunResult,
    OcrToken,
)

LANGUAGE_PACKS = {"en": "eng", "ko": "kor"}
SUPPORTED_PSM_MODES = frozenset({3, 4, 6, 7, 11, 12, 13})
TESSDATA_CANDIDATES = (
    Path("/usr/share/tesseract-ocr/5/tessdata"),
    Path("/usr/share/tessdata"),
    Path("/usr/local/share/tessdata"),
)


@dataclass(frozen=True, slots=True)
class TesseractInstallation:
    binary_version: str
    language_versions: dict[str, str]


def _pytesseract_module() -> Any:
    try:
        import pytesseract  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:
        raise OcrConfigurationError(
            "Tesseract is unavailable; install the local binary and language packs."
        ) from exc
    return pytesseract


def _default_languages() -> Sequence[str]:
    try:
        return _pytesseract_module().get_languages(config="")
    except Exception as exc:
        message = "Installed Tesseract languages could not be inspected."
        raise OcrConfigurationError(message) from exc


def _default_version() -> str:
    try:
        return str(_pytesseract_module().get_tesseract_version())
    except Exception as exc:
        raise OcrConfigurationError("The Tesseract version could not be inspected.") from exc


def _default_tessdata_root() -> Path:
    configured = os.environ.get("TESSDATA_PREFIX")
    candidates = (Path(configured),) if configured else TESSDATA_CANDIDATES
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise OcrConfigurationError("The Tesseract language-data directory could not be found.")


def _language_data_versions(packs: Sequence[str]) -> dict[str, str]:
    root = _default_tessdata_root()
    versions: dict[str, str] = {}
    for pack in packs:
        path = root / f"{pack}.traineddata"
        try:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise OcrConfigurationError(
                f"Missing Tesseract language-data file: {pack}.traineddata."
            ) from exc
        versions[f"language_{pack}"] = f"sha256:{digest.hexdigest()}"
    return versions


def inspect_tesseract_installation(
    required_packs: Sequence[str] = tuple(LANGUAGE_PACKS.values()),
) -> TesseractInstallation:
    """Verify the local binary and required traineddata without network access."""

    installed = set(_default_languages())
    missing = set(required_packs) - installed
    if missing:
        raise OcrConfigurationError(
            f"Missing required Tesseract language pack(s): {', '.join(sorted(missing))}."
        )
    return TesseractInstallation(
        binary_version=_default_version(),
        language_versions=_language_data_versions(required_packs),
    )


def _default_runner(image: Image.Image, **kwargs: Any) -> dict[str, list[Any]]:
    module = _pytesseract_module()
    try:
        return module.image_to_data(image, output_type=module.Output.DICT, **kwargs)
    except Exception as exc:
        raise OcrConfigurationError("Tesseract execution failed.") from exc


def _value(data: dict[str, list[Any]], key: str, index: int, default: Any = 0) -> Any:
    values = data.get(key, [])
    return values[index] if index < len(values) else default


def _tokens(data: dict[str, list[Any]]) -> tuple[OcrToken, ...]:
    tokens: list[OcrToken] = []
    for index, raw_text in enumerate(data.get("text", [])):
        text = str(raw_text).strip()
        confidence = float(_value(data, "conf", index, -1))
        if not text or confidence < 0:
            continue
        left = max(0, int(_value(data, "left", index)))
        top = max(0, int(_value(data, "top", index)))
        width = max(0, int(_value(data, "width", index)))
        height = max(0, int(_value(data, "height", index)))
        hierarchy = tuple(
            int(_value(data, key, index))
            for key in ("page_num", "block_num", "par_num", "line_num", "word_num")
        )
        tokens.append(
            OcrToken(
                text=text,
                confidence=max(0.0, min(1.0, confidence / 100.0)),
                bounding_box=BoundingBox(left, top, left + width, top + height),
                hierarchy=hierarchy,
            )
        )
    return tuple(tokens)


class TesseractOcrEngine(OcrEngine):
    engine_name = "tesseract"

    def __init__(
        self,
        *,
        runner: Callable[..., dict[str, list[Any]]] = _default_runner,
        languages: Callable[[], Sequence[str]] = _default_languages,
        version: Callable[[], str] = _default_version,
        language_versions: Callable[[Sequence[str]], dict[str, str]] = _language_data_versions,
    ) -> None:
        self._runner = runner
        self._installed_languages = languages
        self._version = version
        self._language_versions = language_versions

    @property
    def metadata(self) -> EngineMetadata:
        return EngineMetadata("tesseract", self._version())

    @property
    def supported_languages(self) -> frozenset[str]:
        return frozenset(LANGUAGE_PACKS)

    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        self.validate_configuration(configuration)
        if not image_path.is_file():
            raise OcrConfigurationError("The Tesseract input image does not exist.")
        requested_packs = tuple(LANGUAGE_PACKS[item] for item in configuration.languages)
        missing = set(requested_packs) - set(self._installed_languages())
        if missing:
            raise OcrConfigurationError(
                f"Missing Tesseract language pack(s): {', '.join(sorted(missing))}."
            )
        binary_version = self._version()
        language_versions = self._language_versions(requested_packs)
        psm = int(configuration.options.get("psm", 6))
        if psm not in SUPPORTED_PSM_MODES:
            raise OcrConfigurationError(f"Unsupported Tesseract PSM mode: {psm}.")
        config = f"--psm {psm}"
        started = perf_counter()
        try:
            with Image.open(image_path) as image:
                data = self._runner(image, lang="+".join(requested_packs), config=config)
        except OcrConfigurationError:
            raise
        except Exception as exc:
            raise OcrConfigurationError("Tesseract could not read the input image.") from exc
        duration_ms = round((perf_counter() - started) * 1000)
        metadata = EngineMetadata(
            "tesseract",
            binary_version,
            language_versions,
        )
        return OcrRunResult(
            tokens=_tokens(data),
            metadata=metadata,
            configuration=configuration,
            duration_ms=duration_ms,
            raw_output=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        )
