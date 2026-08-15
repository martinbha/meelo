from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.crypto import encrypt_model_field
from apps.processing.models import SourceDocument

from .contracts import EngineMetadata, OcrConfiguration, OcrRunResult
from .models import OcrRun
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
