from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.core.key_management import get_user_data_key, load_master_key
from apps.processing.models import SourceDocument

from .contracts import OcrConfiguration, OcrEngine, OcrError
from .models import OcrRun
from .paddle import PaddleOcrEngine
from .preprocessing import PreprocessingSettings, preprocess_image
from .services import record_failed_run, record_successful_run
from .tesseract import TesseractOcrEngine


class OcrPipelineError(RuntimeError):
    """The local OCR phase could not produce parseable results."""


@dataclass(frozen=True, slots=True)
class EnginePlan:
    engine: OcrEngine
    configuration: OcrConfiguration


ParserHandoff = Callable[[SourceDocument, Sequence[OcrRun]], bool]
DEFAULT_PREPROCESSING_SETTINGS = PreprocessingSettings(scale=2.0)


def _default_handoff(document: SourceDocument, runs: Sequence[OcrRun]) -> bool:
    return any(run.tokens.exists() for run in runs)


def default_engine_plans() -> tuple[EnginePlan, ...]:
    return (
        EnginePlan(PaddleOcrEngine(), OcrConfiguration(("ko",), {"device": "cpu"})),
        EnginePlan(TesseractOcrEngine(), OcrConfiguration(("ko", "en"), {"psm": 6})),
    )


def orchestrate_document_ocr(
    *,
    document: SourceDocument,
    source_path: Path,
    user: Any,
    data_key: bytes,
    key_version: int,
    plans: Sequence[EnginePlan],
    parser_handoff: ParserHandoff = _default_handoff,
    preprocessing_settings: PreprocessingSettings = DEFAULT_PREPROCESSING_SETTINGS,
) -> tuple[OcrRun, ...]:
    successful: list[OcrRun] = []
    with preprocess_image(source_path, source_path.parent, preprocessing_settings) as prepared:
        selected = prepared.variant(prepared.selected_variant)
        for plan in plans:
            try:
                result = plan.engine.run(selected.path, plan.configuration)
            except OcrError:
                record_failed_run(
                    document=document,
                    user=user,
                    metadata=plan.engine.metadata,
                    configuration=plan.configuration,
                    error_code="OCR_ENGINE_FAILED",
                    duration_ms=0,
                    data_key=data_key,
                    key_version=key_version,
                )
                continue
            successful.append(
                record_successful_run(
                    document=document,
                    user=user,
                    result=result,
                    data_key=data_key,
                    key_version=key_version,
                    preprocessing=prepared,
                )
            )
    if not successful:
        raise OcrPipelineError("No local OCR engine completed successfully.")
    if not parser_handoff(document, successful):
        raise OcrPipelineError("OCR results could not be handed to parsing.")
    return tuple(successful)


def execute_document_ocr(
    *, document: SourceDocument, source_path: Path, user: Any
) -> tuple[OcrRun, ...]:
    master_key = load_master_key()
    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
    return orchestrate_document_ocr(
        document=document,
        source_path=source_path,
        user=user,
        data_key=data_key,
        key_version=user.encryption_key_version,
        plans=default_engine_plans(),
    )
