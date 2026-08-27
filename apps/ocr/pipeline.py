from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.core import metrics
from apps.core.key_scope import require_data_key, require_search_key
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import DocumentMetadata, NormalizedToken
from apps.parsing.generic import GenericTransactionListParser
from apps.parsing.institutions import build_institution_parsers
from apps.parsing.registry import ParserRegistry, ParserSelection
from apps.processing.models import SourceDocument

from .contracts import BoundingBox, EngineMetadata, OcrConfiguration, OcrEngine, OcrError
from .execution import ClassifiedOcrError, OcrResourceLimits, run_engine_bounded
from .matching import MatchStatus, TokenCandidate, match_engine_tokens
from .models import OcrRun, OcrToken
from .paddle import PaddleOcrEngine
from .preprocessing import PreprocessingSettings, preprocess_image, select_variant
from .services import record_failed_run, record_successful_run
from .tesseract import TesseractOcrEngine
from .tesseract_psm import default_psm


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
            text=token.decrypt_field("normalized_text_encrypted", key=data_key),
            normalized_text=token.decrypt_field("normalized_text_encrypted", key=data_key),
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
    degraded: bool = False,
) -> bool:
    """Parse the OCR runs and store the rows for review.

    Returns whether anything reviewable came out. The import is atomic and
    keyed to the newest run, so a retried job re-parses without creating a
    second copy of the same rows.
    """

    try:
        selection = parse_ocr_runs(document, runs, data_key=data_key)
    except Exception:
        metrics.record(metrics.PARSER_FAILED, parser="unknown", reason="exception")
        raise
    if degraded:
        observations = tuple(
            replace(
                observation,
                confidence_factors={
                    **observation.confidence_factors,
                    "token_confidence": min(
                        float(observation.confidence_factors.get("token_confidence", 0.0)) * 0.75,
                        0.79,
                    ),
                    "degraded_ocr": True,
                    "requires_review": True,
                },
            )
            for observation in selection.observations
        )
        selection = replace(selection, observations=observations)
    if not selection.observations:
        metrics.record(
            metrics.PARSER_FAILED,
            parser=selection.metadata.name,
            reason="no_observations",
        )
        return False
    newest = max(runs, key=lambda run: (run.created_at, str(run.pk))) if runs else None
    import_parser_selection(
        document=document,
        ocr_run=newest,
        selection=selection,
        data_key=data_key,
        key_version=key_version,
        # Asked for by name rather than derived from the data key. The two are
        # separate secrets (specification 22.4), and the scope is what decides
        # this job is entitled to either of them.
        blind_index_key=require_search_key(user=user, document=document),
        actor=user,
    )
    metrics.record(
        metrics.PARSER_SELECTED,
        parser=selection.metadata.name,
        source_type=document.effective_source_type,
    )
    metrics.record(
        metrics.PARSER_ROWS,
        value=len(selection.observations),
        parser=selection.metadata.name,
        status="succeeded",
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
    # A reviewer's correction outranks detection. ``manual_source_override``
    # forces the parser outright rather than nudging the score, because a person
    # who has looked at the screenshot and named the bank is not offering
    # evidence to be weighed against the pixels — they are stating the answer.
    # The same name goes in as the hint so the forced parser's support score
    # reflects that assertion instead of reporting zero confidence in a choice
    # nobody is going to reconsider.
    override = document.institution_override or None
    metadata = DocumentMetadata(
        source_type=document.effective_source_type,
        width=document.image_width,
        height=document.image_height,
        institution_hint=override,
        manual_source_override=override,
        # The upload moment and the user's time zone date every relative and
        # partial row on the screen.
        uploaded_at=document.uploaded_at,
        time_zone=str(settings.TIME_ZONE),
    )
    return build_parser_registry().parse(metadata, tokens)


@lru_cache(maxsize=1)
def _default_engines() -> tuple[OcrEngine, OcrEngine]:
    return PaddleOcrEngine(), TesseractOcrEngine()


def default_engine_plans(
    *, source_type: str = "unknown", tesseract_psm: int | None = None
) -> tuple[EnginePlan, ...]:
    selected_psm = default_psm(source_type) if tesseract_psm is None else tesseract_psm
    paddle, tesseract = _default_engines()
    return (
        EnginePlan(paddle, OcrConfiguration(("ko",), {"device": "cpu"})),
        EnginePlan(tesseract, OcrConfiguration(("ko", "en"), {"psm": selected_psm})),
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
    resource_limits: OcrResourceLimits | None = None,
) -> tuple[OcrRun, ...]:
    successful: list[OcrRun] = []
    failures: list[ClassifiedOcrError] = []
    with preprocess_image(source_path, source_path.parent, preprocessing_settings) as prepared:
        for plan in plans:
            variant_name = select_variant(
                engine=plan.engine.engine_name,
                source_type=document.effective_source_type,
            )
            selected = prepared.variant(variant_name)
            try:
                result = run_engine_bounded(
                    plan.engine,
                    selected.path,
                    plan.configuration,
                    timeout_seconds=engine_timeout_seconds,
                    limits=resource_limits,
                )
            except ClassifiedOcrError as exc:
                failures.append(exc)
                metadata = _safe_engine_metadata(plan.engine)
                metrics.record(
                    metrics.OCR_FAILED,
                    engine=metadata.engine,
                    error_code=exc.code,
                )
                record_failed_run(
                    document=document,
                    user=user,
                    metadata=metadata,
                    configuration=plan.configuration,
                    error_code=exc.code,
                    duration_ms=0,
                    data_key=data_key,
                    key_version=key_version,
                )
                continue
            metrics.record(
                metrics.OCR_DURATION,
                value=result.duration_ms,
                engine=result.metadata.engine,
                status="succeeded",
            )
            successful.append(
                record_successful_run(
                    document=document,
                    user=user,
                    result=result,
                    data_key=data_key,
                    key_version=key_version,
                    preprocessing=prepared.for_variant(variant_name),
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
            degraded=bool(failures) and len(plans) > 1,
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
    # The scope the job handler opened, and nothing else. Unwrapping here as a
    # fallback would authenticate the owner as their own actor — the rule the
    # worker path exists to replace with one that checks the document.
    data_key = require_data_key(user=user)
    limits = OcrResourceLimits(
        timeout_seconds=settings.OCR_ENGINE_TIMEOUT_SECONDS,
        max_threads=settings.OCR_ENGINE_MAX_THREADS,
        memory_bytes=settings.OCR_ENGINE_MEMORY_LIMIT_MB * 1024 * 1024,
    )
    return orchestrate_document_ocr(
        document=document,
        source_path=source_path,
        user=user,
        data_key=data_key,
        key_version=user.encryption_key_version,
        plans=default_engine_plans(source_type=document.effective_source_type),
        engine_timeout_seconds=limits.timeout_seconds,
        resource_limits=limits,
    )
