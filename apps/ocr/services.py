from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.crypto import decrypt_model_field, encrypt_model_field
from apps.processing.models import SourceDocument

from .contracts import EngineMetadata, OcrConfiguration, OcrRunResult
from .models import OcrRun, OcrToken
from .preprocessing import PreprocessingResult


def _encrypted_json(
    run: OcrRun,
    field: str,
    value: Any,
    *,
    data_key: bytes,
    key_version: int,
) -> str:
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return encrypt_model_field(
        run, field, plaintext, key=data_key, key_version=key_version
    )


def _configuration_payload(configuration: OcrConfiguration) -> dict[str, Any]:
    return {"languages": list(configuration.languages), "options": dict(configuration.options)}


def persist_tokens(
    *, run: OcrRun, tokens: tuple[Any, ...], data_key: bytes, key_version: int
) -> list[OcrToken]:
    records: list[OcrToken] = []
    for sequence, token in enumerate(tokens):
        hierarchy = (*token.hierarchy, 0, 0, 0, 0, 0)
        box = token.bounding_box
        record = OcrToken(
            user_id=run.user_id,
            ocr_run=run,
            confidence=token.confidence,
            left=box.left,
            top=box.top,
            right=box.right,
            bottom=box.bottom,
            page_number=hierarchy[0],
            block_number=hierarchy[1],
            paragraph_number=hierarchy[2],
            line_number=hierarchy[3],
            word_number=hierarchy[4],
            sequence=sequence,
        )
        record.text_encrypted = encrypt_model_field(
            record,
            "text_encrypted",
            token.text,
            key=data_key,
            key_version=key_version,
        )
        record.full_clean()
        records.append(record)
    return OcrToken.objects.bulk_create(records)


def serialize_token_for_review(*, token: OcrToken, user: Any, data_key: bytes) -> dict[str, Any]:
    if token.user_id != user.pk:
        raise ValueError("The OCR token does not belong to the requesting user.")
    return {
        "id": str(token.pk),
        "text": decrypt_model_field(token, "text_encrypted", key=data_key),
        "confidence": token.confidence,
        "bounds": {
            "left": token.left,
            "top": token.top,
            "right": token.right,
            "bottom": token.bottom,
        },
        "line": token.line_number,
        "word": token.word_number,
        "sequence": token.sequence,
    }


@transaction.atomic
def record_successful_run(
    *,
    document: SourceDocument,
    user: Any,
    result: OcrRunResult,
    data_key: bytes,
    key_version: int,
    preprocessing: PreprocessingResult | None = None,
) -> OcrRun:
    if document.user_id != user.pk:
        raise ValueError("The OCR run document must belong to the requesting user.")
    completed_at = timezone.now()
    run = OcrRun(
        user=user,
        source_document=document,
        engine=result.metadata.engine,
        engine_version=result.metadata.engine_version,
        model_versions=dict(result.metadata.model_versions),
        languages=list(result.configuration.languages),
        selected_preprocessing_variant=(preprocessing.selected_variant if preprocessing else ""),
        succeeded=True,
        duration_ms=result.duration_ms,
        started_at=completed_at - timedelta(milliseconds=result.duration_ms),
        completed_at=completed_at,
    )
    run.configuration_encrypted = _encrypted_json(
        run,
        "configuration_encrypted",
        _configuration_payload(result.configuration),
        data_key=data_key,
        key_version=key_version,
    )
    if preprocessing is not None:
        run.preprocessing_encrypted = _encrypted_json(
            run,
            "preprocessing_encrypted",
            preprocessing.settings.serializable(),
            data_key=data_key,
            key_version=key_version,
        )
    run.raw_output_encrypted = _encrypted_json(
        run,
        "raw_output_encrypted",
        result.raw_output,
        data_key=data_key,
        key_version=key_version,
    )
    run.full_clean()
    run.save()
    persist_tokens(
        run=run,
        tokens=result.tokens,
        data_key=data_key,
        key_version=key_version,
    )
    return run


@transaction.atomic
def record_failed_run(
    *,
    document: SourceDocument,
    user: Any,
    metadata: EngineMetadata,
    configuration: OcrConfiguration,
    error_code: str,
    duration_ms: int,
    data_key: bytes,
    key_version: int,
) -> OcrRun:
    if document.user_id != user.pk:
        raise ValueError("The OCR run document must belong to the requesting user.")
    completed_at = timezone.now()
    run = OcrRun(
        user=user,
        source_document=document,
        engine=metadata.engine,
        engine_version=metadata.engine_version,
        model_versions=dict(metadata.model_versions),
        languages=list(configuration.languages),
        succeeded=False,
        error_code=error_code[:64],
        duration_ms=max(0, duration_ms),
        started_at=completed_at - timedelta(milliseconds=max(0, duration_ms)),
        completed_at=completed_at,
    )
    run.configuration_encrypted = _encrypted_json(
        run,
        "configuration_encrypted",
        _configuration_payload(configuration),
        data_key=data_key,
        key_version=key_version,
    )
    run.full_clean()
    run.save()
    return run
