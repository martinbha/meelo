from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.core import metrics
from apps.core.crypto import decrypt_model_field
from apps.core.key_management import derive_blind_index_key, get_user_data_key, load_master_key
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import DocumentMetadata, NormalizedToken
from apps.parsing.generic import GenericTransactionListParser
from apps.parsing.institutions import build_institution_parsers
from apps.parsing.registry import ParserRegistry, ParserSelection
from apps.processing.models import SourceDocument

from .contracts import BoundingBox, EngineMetadata, OcrConfiguration, OcrEngine, OcrError
from .execution import ClassifiedOcrError, run_engine_bounded
from .matching import MatchStatus, TokenCandidate, match_engine_tokens
from .models import OcrRun, OcrToken
from .paddle import PaddleOcrEngine
from .preprocessing import PreprocessingSettings, preprocess_image
from .services import record_failed_run, record_successful_run
from .tesseract import TesseractOcrEngine


class OcrPipelineError(RuntimeError):
    """The local OCR phase could not produce parseable results."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class EnginePlan:
    engine: OcrEngine
    configuration: OcrConfiguration


ParserHandoff = Callable[[SourceDocument, Sequence[OcrRun]], bool]
DEFAULT_PREPROCESSING_SETTINGS = PreprocessingSettings(scale=2.0)


def _safe_engine_metadata(engine: OcrEngine) -> EngineMetadata:
    """Engine identity for a run whose engine could not report its own.

    The failure still has to be recorded against an engine, and ``metadata`` is
    the property that just raised. The declared ``engine_name`` is used instead
    of a string derived from the class name, because ``OcrRun.engine`` accepts
    only the two engines in specification 6.5 — deriving one would write a value
    outside that set and turn a recorded OCR failure into a validation error
    about the record, losing the original cause.
    """

    try:
        return engine.metadata
    except OcrError:
        return EngineMetadata(engine.engine_name, "unavailable")


def _run_candidates(run: OcrRun, data_key: bytes) -> tuple[TokenCandidate, ...]:
    return tuple(
        TokenCandidate(
            engine=run.engine,
            text=decrypt_model_field(token, "normalized_text_encrypted", key=data_key),
            normalized_text=decrypt_model_field(token, "normalized_text_encrypted", key=data_key),
            confidence=token.confidence,
            bounding_box=BoundingBox(token.left, token.top, token.right, token.bottom),
        )
        for token in OcrToken.objects.filter(ocr_run=run).order_by("sequence")
    )


def tokens_for_parsing(runs: Sequence[OcrRun], *, data_key: bytes) -> tuple[NormalizedToken, ...]:
    if not runs:
        return ()
    candidates = [_run_candidates(run, data_key) for run in runs]
    grouped: list[tuple[str, float, BoundingBox, tuple[str, ...]]] = []
    if len(candidates) == 1:
        grouped.extend(
            (token.normalized_text, token.confidence, token.bounding_box, (token.engine,))
            for token in candidates[0]
        )
    else:
        for group in match_engine_tokens(candidates[0], candidates[1]):
            if group.status == MatchStatus.MATCHED:
                strongest = max(group.tokens, key=lambda token: token.confidence)
                grouped.append(
                    (
                        strongest.normalized_text,
                        strongest.confidence,
                        group.region,
                        tuple(token.engine for token in group.tokens),
                    )
                )
            else:
                grouped.extend(
                    (
                        token.normalized_text,
                        token.confidence,
                        token.bounding_box,
                        (token.engine,),
                    )
                    for token in group.tokens
                )
        for extra in candidates[2:]:
            grouped.extend(
                (token.normalized_text, token.confidence, token.bounding_box, (token.engine,))
                for token in extra
            )
    grouped.sort(key=lambda item: (item[2].top, item[2].left, item[0]))
    return tuple(
        NormalizedToken(text, confidence, box, sequence, engines)
        for sequence, (text, confidence, box, engines) in enumerate(grouped)
    )


def import_document_observations(
    *,
    document: SourceDocument,
    runs: Sequence[OcrRun],
    data_key: bytes,
    key_version: int,
    user: Any,
) -> bool:
    """Parse the OCR runs and store the rows for review.

    Returns whether anything reviewable came out. The import is atomic and
    keyed to the newest run, so a retried job re-parses without creating a
    second copy of the same rows.
    """

    selection = parse_ocr_runs(document, runs, data_key=data_key)
    if not selection.observations:
        return False
    newest = max(runs, key=lambda run: (run.created_at, str(run.pk))) if runs else None
    import_parser_selection(
        document=document,
        ocr_run=newest,
        selection=selection,
        data_key=data_key,
        key_version=key_version,
        # Derived rather than passed in, so every part of the system agrees on
        # one search key per user and a rule written from review can find the
        # rows that produced it.
        blind_index_key=derive_blind_index_key(data_key),
        actor=user,
    )
    return True


def build_parser_registry() -> ParserRegistry:
    """The registry the pipeline parses with: every institution, then generic."""

    registry = ParserRegistry(generic_parser=GenericTransactionListParser())
    for parser in build_institution_parsers():
        registry.register(parser)
    return registry


def parse_ocr_runs(
    document: SourceDocument, runs: Sequence[OcrRun], *, data_key: bytes
) -> ParserSelection:
    tokens = tokens_for_parsing(runs, data_key=data_key)
    metadata = DocumentMetadata(
        source_type=document.source_type,
        width=document.image_width,
        height=document.image_height,
        # The upload moment and the user's time zone date every relative and
        # partial row on the screen.
        uploaded_at=document.uploaded_at,
        time_zone=str(settings.TIME_ZONE),
    )
    return build_parser_registry().parse(metadata, tokens)


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
    parser_handoff: ParserHandoff | None = None,
    preprocessing_settings: PreprocessingSettings = DEFAULT_PREPROCESSING_SETTINGS,
    engine_timeout_seconds: float = 120.0,
) -> tuple[OcrRun, ...]:
    successful: list[OcrRun] = []
    failures: list[ClassifiedOcrError] = []
    with preprocess_image(source_path, source_path.parent, preprocessing_settings) as prepared:
        selected = prepared.variant(prepared.selected_variant)
        for plan in plans:
            try:
                result = run_engine_bounded(
                    plan.engine,
                    selected.path,
                    plan.configuration,
                    timeout_seconds=engine_timeout_seconds,
                )
            except ClassifiedOcrError as exc:
                failures.append(exc)
                record_failed_run(
                    document=document,
                    user=user,
                    metadata=_safe_engine_metadata(plan.engine),
                    configuration=plan.configuration,
                    error_code=exc.code,
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
        retryable = any(failure.retryable for failure in failures)
        code = failures[0].code if len(failures) == 1 else "OCR_ALL_ENGINES_FAILED"
        raise OcrPipelineError(
            "No local OCR engine completed successfully.",
            code=code,
            retryable=retryable,
        )
    parsing_succeeded = (
        parser_handoff(document, successful)
        if parser_handoff is not None
        else import_document_observations(
            document=document,
            runs=successful,
            data_key=data_key,
            key_version=key_version,
            user=user,
        )
    )
    if not parsing_succeeded:
        raise OcrPipelineError(
            "OCR results could not be handed to parsing.",
            code="OCR_PARSE_HANDOFF_FAILED",
            retryable=False,
        )
    return tuple(successful)


def execute_document_ocr(
    *, document: SourceDocument, source_path: Path, user: Any
) -> tuple[OcrRun, ...]:
    master_key = load_master_key()
    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
    # Timed here rather than per engine: this is the span a slow document is
    # actually slow for, and the outcome label separates a fast failure from a
    # fast success.
    with metrics.timed(metrics.OCR_DURATION, document_id=str(document.pk)):
        try:
            runs = orchestrate_document_ocr(
                document=document,
                source_path=source_path,
                user=user,
                data_key=data_key,
                key_version=user.encryption_key_version,
                plans=default_engine_plans(),
            )
        except Exception:
            metrics.record(metrics.OCR_FAILED, document_id=str(document.pk))
            raise
    return runs
