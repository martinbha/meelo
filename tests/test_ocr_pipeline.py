import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.ocr.contracts import (
    BoundingBox,
    EngineMetadata,
    OcrConfiguration,
    OcrConfigurationError,
    OcrEngine,
    OcrRunResult,
    OcrToken,
)
from apps.ocr.models import OcrRun
from apps.ocr.pipeline import EnginePlan, OcrPipelineError, orchestrate_document_ocr
from apps.ocr.preprocessing import PreprocessingSettings
from apps.processing.models import SourceDocument


class StubEngine(OcrEngine):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.fails = fails

    @property
    def metadata(self) -> EngineMetadata:
        return EngineMetadata(self.name, "1", {"model": "1"})

    @property
    def supported_languages(self) -> frozenset[str]:
        return frozenset({"ko"})

    def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
        if self.fails:
            raise OcrConfigurationError("engine failed")
        assert image_path.name == "ocr-threshold.png"
        return OcrRunResult(
            tokens=(OcrToken(self.name, 0.9, BoundingBox(1, 2, 10, 12)),),
            metadata=self.metadata,
            configuration=configuration,
            duration_ms=3,
            raw_output=self.name,
        )


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("pipeline-owner@example.com", password="password")


def make_document(user: Any) -> SourceDocument:
    return SourceDocument.objects.create(
        user=user,
        file_sha256="2" * 64,
        original_filename_encrypted="encrypted",
        mime_type="image/png",
        file_size=20,
    )


@pytest.mark.django_db
def test_pipeline_persists_partial_success_and_hands_off_after_ocr(
    user: Any, tmp_path: Path
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    document = make_document(user)
    handed_off: list[list[str]] = []

    def handoff(doc: SourceDocument, completed: Sequence[OcrRun]) -> bool:
        handed_off.append([run.engine for run in completed])
        return True

    runs = orchestrate_document_ocr(
        document=document,
        source_path=source,
        user=user,
        data_key=os.urandom(32),
        key_version=1,
        plans=(
            EnginePlan(StubEngine("primary"), OcrConfiguration(("ko",))),
            EnginePlan(StubEngine("fallback", fails=True), OcrConfiguration(("ko",))),
        ),
        parser_handoff=handoff,
        preprocessing_settings=PreprocessingSettings(),
    )

    assert [run.engine for run in runs] == ["primary"]
    assert handed_off == [["primary"]]
    assert OcrRun.objects.filter(source_document=document).count() == 2
    assert OcrRun.objects.get(source_document=document, succeeded=False).error_code == (
        "OCR_ENGINE_FAILED"
    )
    assert runs[0].tokens.count() == 1


@pytest.mark.django_db
def test_pipeline_rejects_total_failure_and_parser_handoff_failure(
    user: Any, tmp_path: Path
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    document = make_document(user)
    key = os.urandom(32)

    with pytest.raises(OcrPipelineError, match="No local OCR"):
        orchestrate_document_ocr(
            document=document,
            source_path=source,
            user=user,
            data_key=key,
            key_version=1,
            plans=(EnginePlan(StubEngine("failed", fails=True), OcrConfiguration(("ko",))),),
            preprocessing_settings=PreprocessingSettings(),
        )

    with pytest.raises(OcrPipelineError, match="handed to parsing"):
        orchestrate_document_ocr(
            document=document,
            source_path=source,
            user=user,
            data_key=key,
            key_version=1,
            plans=(EnginePlan(StubEngine("success"), OcrConfiguration(("ko",))),),
            parser_handoff=lambda doc, runs: False,
            preprocessing_settings=PreprocessingSettings(),
        )
