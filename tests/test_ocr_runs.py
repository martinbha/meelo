import json
import os
from pathlib import Path
from typing import Any

import pytest
from django.contrib import admin

from apps.core.crypto import decrypt_model_field
from apps.ocr.contracts import EngineMetadata, OcrConfiguration, OcrRunResult
from apps.ocr.models import OcrRun
from apps.ocr.preprocessing import PreprocessingSettings, preprocess_image
from apps.ocr.services import record_failed_run, record_successful_run
from apps.processing.models import SourceDocument


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("ocr-owner@example.com", password="password")


def make_document(user: Any, suffix: str = "one") -> SourceDocument:
    return SourceDocument.objects.create(
        user=user,
        file_sha256=suffix.rjust(64, "0"),
        original_filename_encrypted="encrypted",
        mime_type="image/png",
        file_size=20,
    )


@pytest.mark.django_db
def test_successful_runs_encrypt_payloads_and_preserve_reproducibility(
    user: Any, tmp_path: Path
) -> None:
    document = make_document(user)
    data_key = os.urandom(32)
    source = tmp_path / "source.png"
    from PIL import Image

    Image.new("RGB", (20, 20), "white").save(source)
    configuration = OcrConfiguration(("ko",), {"device": "cpu"})
    result = OcrRunResult(
        tokens=(),
        metadata=EngineMetadata("paddleocr", "3.3", {"paddlepaddle": "3.3"}),
        configuration=configuration,
        duration_ms=25,
        raw_output='{"text":"민감"}',
    )
    with preprocess_image(source, tmp_path / "work", PreprocessingSettings()) as preprocessing:
        run = record_successful_run(
            document=document,
            user=user,
            result=result,
            data_key=data_key,
            key_version=1,
            preprocessing=preprocessing,
        )

    assert run.succeeded is True
    assert run.selected_preprocessing_variant == "threshold"
    assert run.raw_output_encrypted != result.raw_output
    raw = decrypt_model_field(run, "raw_output_encrypted", key=data_key)
    assert json.loads(raw) == result.raw_output
    settings = decrypt_model_field(run, "preprocessing_encrypted", key=data_key)
    assert json.loads(settings)["threshold"] == 170
    assert OcrRun.objects.get(source_document=document) == run


@pytest.mark.django_db
def test_multiple_and_failed_runs_keep_only_safe_failure_metadata(user: Any) -> None:
    document = make_document(user)
    key = os.urandom(32)
    configuration = OcrConfiguration(("en",), {"psm": 6})
    metadata = EngineMetadata("tesseract", "5.5")

    first = record_failed_run(
        document=document,
        user=user,
        metadata=metadata,
        configuration=configuration,
        error_code="LANGUAGE_PACK_MISSING",
        duration_ms=7,
        data_key=key,
        key_version=1,
    )
    second = record_failed_run(
        document=document,
        user=user,
        metadata=metadata,
        configuration=configuration,
        error_code="ENGINE_TIMEOUT",
        duration_ms=9,
        data_key=key,
        key_version=1,
    )

    assert OcrRun.objects.filter(source_document=document).count() == 2
    assert first.raw_output_encrypted == ""
    assert second.raw_output_encrypted == ""
    assert first.error_code == "LANGUAGE_PACK_MISSING"
    assert "psm" not in first.configuration_encrypted


@pytest.mark.django_db
def test_run_service_and_model_reject_cross_owner_documents(user: Any) -> None:
    other = type(user).objects.create_user("other-ocr@example.com", password="password")
    document = make_document(other, "two")
    with pytest.raises(ValueError, match="requesting user"):
        record_failed_run(
            document=document,
            user=user,
            metadata=EngineMetadata("tesseract", "5"),
            configuration=OcrConfiguration(("en",)),
            error_code="FAILED",
            duration_ms=0,
            data_key=os.urandom(32),
            key_version=1,
        )


def test_ocr_admin_never_lists_encrypted_payloads() -> None:
    model_admin = admin.site._registry[OcrRun]
    assert "raw_output_encrypted" not in model_admin.list_display
    assert "configuration_encrypted" not in model_admin.list_display
