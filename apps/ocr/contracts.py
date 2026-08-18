from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


class OcrError(RuntimeError):
    """Base error raised by local OCR adapters."""


class OcrConfigurationError(OcrError):
    """The requested engine configuration cannot be used."""


class UnsupportedLanguageError(OcrConfigurationError):
    """An OCR engine does not support a requested language."""


@dataclass(frozen=True, slots=True)
class BoundingBox:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if min(self.left, self.top, self.right, self.bottom) < 0:
            raise ValueError("Bounding-box coordinates cannot be negative.")
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("Bounding-box edges are out of order.")


@dataclass(frozen=True, slots=True)
class OcrToken:
    text: str
    confidence: float
    bounding_box: BoundingBox
    hierarchy: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCR confidence must be between zero and one.")


@dataclass(frozen=True, slots=True)
class EngineMetadata:
    engine: str
    engine_version: str
    model_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_versions", MappingProxyType(dict(self.model_versions)))


@dataclass(frozen=True, slots=True)
class OcrConfiguration:
    languages: tuple[str, ...]
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(language.strip().lower() for language in self.languages))
        if not normalized or any(not language for language in normalized):
            raise OcrConfigurationError("At least one valid OCR language is required.")
        object.__setattr__(self, "languages", normalized)
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class OcrRunResult:
    tokens: tuple[OcrToken, ...]
    metadata: EngineMetadata
    configuration: OcrConfiguration
    duration_ms: int
    raw_output: str = ""

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("OCR duration cannot be negative.")

    @property
    def text(self) -> str:
        return " ".join(token.text for token in self.tokens if token.text)


class OcrEngine(ABC):
    """Adapter boundary that prevents engine-specific output leaking downstream."""

    #: Which engine of specification 6.5 this adapter is, as written to
    #: ``OcrRun.engine``. Declared rather than derived from the class name: the
    #: column holds a fixed set of values, and the one moment the name is needed
    #: without asking the engine for it is the moment the engine has already
    #: failed to answer.
    engine_name: str

    @property
    @abstractmethod
    def metadata(self) -> EngineMetadata:
        raise NotImplementedError

    @property
    @abstractmethod
    def supported_languages(self) -> frozenset[str]:
        raise NotImplementedError

    def validate_configuration(self, configuration: OcrConfiguration) -> None:
        unsupported = set(configuration.languages) - self.supported_languages
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise UnsupportedLanguageError(f"Unsupported OCR language(s): {names}.")

    @abstractmethod
    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        raise NotImplementedError
