from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .contracts import OcrConfigurationError


@dataclass(frozen=True, slots=True)
class PreprocessingSettings:
    scale: float = 1.0
    rotation: int = 0
    crop: tuple[int, int, int, int] | None = None
    threshold: int = 170
    include_grayscale: bool = True
    include_threshold: bool = True

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError("Preprocessing scale must be positive.")
        if self.rotation not in {0, 90, 180, 270}:
            raise ValueError("Preprocessing rotation must be 0, 90, 180, or 270 degrees.")
        if not 0 <= self.threshold <= 255:
            raise ValueError("Preprocessing threshold must be between 0 and 255.")
        if self.crop is not None:
            left, top, right, bottom = self.crop
            if min(self.crop) < 0 or right <= left or bottom <= top:
                raise ValueError("Preprocessing crop bounds are invalid.")

    def serializable(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreprocessedVariant:
    name: str
    path: Path
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PreprocessingResult:
    settings: PreprocessingSettings
    variants: tuple[PreprocessedVariant, ...]
    selected_variant: str

    def variant(self, name: str) -> PreprocessedVariant:
        try:
            return next(item for item in self.variants if item.name == name)
        except StopIteration as exc:
            raise OcrConfigurationError(f"Unknown preprocessing variant: {name}.") from exc


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_variant(image: Image.Image, directory: Path, name: str) -> PreprocessedVariant:
    path = directory / f"ocr-{name}.png"
    image.save(path, format="PNG", optimize=False, compress_level=9)
    return PreprocessedVariant(name, path, image.width, image.height, _digest(path))


def _normalized_image(source_path: Path, settings: PreprocessingSettings) -> Image.Image:
    try:
        with Image.open(source_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError) as exc:
        raise OcrConfigurationError("The source image could not be preprocessed.") from exc
    if settings.crop is not None:
        if settings.crop[2] > image.width or settings.crop[3] > image.height:
            raise OcrConfigurationError("Preprocessing crop exceeds the source image.")
        image = image.crop(settings.crop)
    if settings.rotation:
        image = image.rotate(settings.rotation, expand=True)
    if settings.scale != 1.0:
        size = (
            max(1, round(image.width * settings.scale)),
            max(1, round(image.height * settings.scale)),
        )
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image


@contextmanager
def preprocess_image(
    source_path: Path,
    directory: Path,
    settings: PreprocessingSettings,
) -> Iterator[PreprocessingResult]:
    if not source_path.is_file():
        raise OcrConfigurationError("The preprocessing source image does not exist.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    variants: list[PreprocessedVariant] = []
    try:
        normalized = _normalized_image(source_path, settings)
        variants.append(_save_variant(normalized, directory, "normalized"))
        grayscale = ImageOps.grayscale(normalized)
        if settings.include_grayscale:
            variants.append(_save_variant(grayscale, directory, "grayscale"))
        if settings.include_threshold:
            threshold = grayscale.point(lambda value: 255 if value >= settings.threshold else 0)
            variants.append(_save_variant(threshold, directory, "threshold"))
        selected = "threshold" if settings.include_threshold else variants[-1].name
        yield PreprocessingResult(settings, tuple(variants), selected)
    finally:
        for variant in variants:
            variant.path.unlink(missing_ok=True)
