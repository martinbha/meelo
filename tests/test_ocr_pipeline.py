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
from apps.ocr.pipeline import (
    EnginePlan,
    OcrPipelineError,
    orchestrate_document_ocr,
    parse_ocr_runs,
)
from apps.ocr.preprocessing import PreprocessingSettings
from apps.processing.models import SourceDocument


class StubEngine(OcrEngine):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.engine_name = name
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


class UninspectableEngine(StubEngine):
    @property
    def metadata(self) -> EngineMetadata:
        raise OcrConfigurationError("runtime unavailable")


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
            EnginePlan(StubEngine("paddleocr"), OcrConfiguration(("ko",))),
            EnginePlan(StubEngine("tesseract", fails=True), OcrConfiguration(("ko",))),
        ),
        parser_handoff=handoff,
        preprocessing_settings=PreprocessingSettings(),
    )

    assert [run.engine for run in runs] == ["paddleocr"]
    assert handed_off == [["paddleocr"]]
    assert OcrRun.objects.filter(source_document=document).count() == 2
    assert OcrRun.objects.get(source_document=document, succeeded=False).error_code == (
        "OCR_CONFIGURATION_INVALID"
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

    with pytest.raises(OcrPipelineError, match="No local OCR") as failure:
        orchestrate_document_ocr(
            document=document,
            source_path=source,
            user=user,
            data_key=key,
            key_version=1,
            plans=(EnginePlan(StubEngine("paddleocr", fails=True), OcrConfiguration(("ko",))),),
            preprocessing_settings=PreprocessingSettings(),
        )
    assert failure.value.retryable is False
    assert failure.value.code == "OCR_CONFIGURATION_INVALID"

    with pytest.raises(OcrPipelineError, match="handed to parsing"):
        orchestrate_document_ocr(
            document=document,
            source_path=source,
            user=user,
            data_key=key,
            key_version=1,
            plans=(EnginePlan(StubEngine("paddleocr"), OcrConfiguration(("ko",))),),
            parser_handoff=lambda doc, runs: False,
            preprocessing_settings=PreprocessingSettings(),
        )


@pytest.mark.django_db
def test_default_handoff_runs_generic_parser_over_persisted_tokens(
    user: Any, tmp_path: Path
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (300, 50), "white").save(source)
    document = make_document(user)
    key = os.urandom(32)

    class RowEngine(StubEngine):
        def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
            return OcrRunResult(
                tokens=(
                    OcrToken("2026.8.15", 0.9, BoundingBox(0, 10, 60, 20)),
                    OcrToken("Cafe", 0.9, BoundingBox(70, 10, 120, 20)),
                    OcrToken("출금", 0.9, BoundingBox(130, 10, 160, 20)),
                    OcrToken("₩4,200", 0.9, BoundingBox(170, 10, 230, 20)),
                ),
                metadata=self.metadata,
                configuration=configuration,
                duration_ms=2,
            )

    runs = orchestrate_document_ocr(
        document=document,
        source_path=source,
        user=user,
        data_key=key,
        key_version=1,
        plans=(EnginePlan(RowEngine("paddleocr"), OcrConfiguration(("ko",))),),
        preprocessing_settings=PreprocessingSettings(),
    )
    selection = parse_ocr_runs(document, runs, data_key=key)

    assert selection.metadata.name == "generic"
    assert len(selection.observations) == 1
    assert selection.observations[0].merchant == "cafe"
    assert str(selection.observations[0].amount) == "4200"


@pytest.mark.django_db
def test_handoff_selects_an_institution_parser_for_a_known_app(user: Any, tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (300, 80), "white").save(source)
    document = make_document(user)
    document.source_type = "bank_transaction_list"
    document.save(update_fields=["source_type"])
    key = os.urandom(32)

    class TossEngine(StubEngine):
        def run(self, image_path: Path, configuration: OcrConfiguration) -> OcrRunResult:
            return OcrRunResult(
                tokens=(
                    OcrToken("토스뱅크", 0.9, BoundingBox(0, 10, 60, 24)),
                    OcrToken("거래내역", 0.9, BoundingBox(70, 10, 130, 24)),
                    # A partial date, dated from the document's upload moment.
                    OcrToken("08.15", 0.9, BoundingBox(0, 50, 60, 64)),
                    OcrToken("스타벅스", 0.9, BoundingBox(70, 50, 140, 64)),
                    OcrToken("출금", 0.9, BoundingBox(150, 50, 190, 64)),
                    OcrToken("4,200원", 0.9, BoundingBox(200, 50, 280, 64)),
                ),
                metadata=self.metadata,
                configuration=configuration,
                duration_ms=2,
            )

    runs = orchestrate_document_ocr(
        document=document,
        source_path=source,
        user=user,
        data_key=key,
        key_version=1,
        plans=(EnginePlan(TossEngine("paddleocr"), OcrConfiguration(("ko",))),),
        preprocessing_settings=PreprocessingSettings(),
    )
    selection = parse_ocr_runs(document, runs, data_key=key)

    assert selection.metadata.name == "toss_bank"
    assert selection.support.score > 0.5
    observation = selection.observations[0]
    assert observation.merchant == "스타벅스"
    assert observation.amount_minor == 4200
    assert observation.direction == "debit"
    # The upload timestamp supplied the missing year, so the row is reviewable.
    assert observation.occurred_on is not None
    assert observation.occurred_on.year == document.uploaded_at.year
    assert observation.confidence_factors["requires_review"] is True


@pytest.mark.django_db
def test_pipeline_records_failure_when_engine_metadata_is_unavailable(
    user: Any, tmp_path: Path
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    document = make_document(user)

    with pytest.raises(OcrPipelineError) as failure:
        orchestrate_document_ocr(
            document=document,
            source_path=source,
            user=user,
            data_key=os.urandom(32),
            key_version=1,
            plans=(
                EnginePlan(UninspectableEngine("paddleocr", fails=True), OcrConfiguration(("ko",))),
            ),
            preprocessing_settings=PreprocessingSettings(),
        )

    stored = OcrRun.objects.get(source_document=document)
    assert failure.value.retryable is False
    assert stored.engine == "paddleocr"
    assert stored.engine_version == "unavailable"
    assert stored.error_code == "OCR_CONFIGURATION_INVALID"
