from pathlib import Path

import pytest
from django.test import override_settings
from PIL import Image

from apps.ocr.contracts import BoundingBox, EngineMetadata, OcrConfiguration, OcrRunResult
from apps.ocr.fixture_harness import load_fixture_cases, render_debug_overlay, run_fixture_suite
from apps.ocr.matching import MatchStatus, TokenCandidate, TokenGroup

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "ocr"


def test_fixture_suite_tracks_duration_and_key_field_regressions() -> None:
    cases = load_fixture_cases(FIXTURE_ROOT)

    def runner(path: Path) -> OcrRunResult:
        assert path.name == "sanitized-basic.pbm"
        return OcrRunResult((), EngineMetadata("fixture", "1"), OcrConfiguration(("ko",)), 12)

    metrics = run_fixture_suite(
        cases,
        runner=runner,
        field_extractor=lambda result: {
            "amount": "4200 KRW",
            "date": "2026-08-16",
            "merchant": "cafe",
        },
    )

    assert len(cases) == 1
    assert [token.text for token in cases[0].expected_tokens] == ["Cafe", "₩4,200"]
    assert metrics[0].duration_ms == 12
    assert metrics[0].field_accuracy == {"amount": True, "date": False, "merchant": True}
    assert metrics[0].accuracy == pytest.approx(2 / 3)


def conflict_group() -> TokenGroup:
    candidate = TokenCandidate("primary", "4200", "4200", 0.9, BoundingBox(1, 1, 12, 6))
    return TokenGroup(MatchStatus.CONFLICT, (candidate,), candidate.bounding_box, 50)


@override_settings(DEBUG=True)
def test_debug_overlay_renders_regions_only_in_development(tmp_path: Path) -> None:
    case = load_fixture_cases(FIXTURE_ROOT)[0]
    output = render_debug_overlay(case.image_path, (conflict_group(),), tmp_path / "overlay.png")

    assert output.is_file()
    with Image.open(output) as image:
        assert image.mode == "RGB"
        assert image.getbbox() is not None


@override_settings(DEBUG=False)
def test_debug_overlay_is_unavailable_in_production(tmp_path: Path) -> None:
    case = load_fixture_cases(FIXTURE_ROOT)[0]
    with pytest.raises(PermissionError, match="disabled"):
        render_debug_overlay(case.image_path, (conflict_group(),), tmp_path / "overlay.png")
