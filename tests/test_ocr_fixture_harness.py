import json
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
    assert cases[0].institution == "fixture_bank"
    assert cases[0].source_type == "bank_transaction_list"
    assert cases[0].expected_rows[0]["direction"] == "debit"
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
    assert case.image_path is not None
    output = render_debug_overlay(case.image_path, (conflict_group(),), tmp_path / "overlay.png")

    assert output.is_file()
    with Image.open(output) as image:
        assert image.mode == "RGB"
        assert image.getbbox() is not None


@override_settings(DEBUG=False)
def test_debug_overlay_is_unavailable_in_production(tmp_path: Path) -> None:
    case = load_fixture_cases(FIXTURE_ROOT)[0]
    assert case.image_path is not None
    with pytest.raises(PermissionError, match="disabled"):
        render_debug_overlay(case.image_path, (conflict_group(),), tmp_path / "overlay.png")


def test_loader_supports_token_only_expected_failure_fixtures(tmp_path: Path) -> None:
    manifest = {
        "name": "unreadable-row",
        "institution": "fixture_bank",
        "source_type": "bank_transaction_list",
        "tokens": [{"text": "???", "bounds": [1, 1, 3, 3]}],
        "expected_rows": [],
        "expected_failure": {"code": "UNREADABLE_ROW"},
    }
    (tmp_path / "case.json").write_text(json.dumps(manifest), encoding="utf-8")

    case = load_fixture_cases(tmp_path)[0]

    assert case.image_path is None
    assert case.expected_failure == "UNREADABLE_ROW"


def test_loader_reports_precise_manifest_errors(tmp_path: Path) -> None:
    manifest = {
        "name": "bad-confidence",
        "institution": "fixture_bank",
        "source_type": "bank_transaction_list",
        "tokens": [{"text": "row", "bounds": [1, 1, 3, 3]}],
        "expected_rows": [{"confidence": {"min": 0.9, "max": 0.4}}],
    }
    (tmp_path / "case.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=r"expected_rows\[0\]\.confidence"):
        load_fixture_cases(tmp_path)

    manifest["expected_rows"] = [{"merhcant": "typo"}]
    (tmp_path / "case.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=r"unknown field.*merhcant"):
        load_fixture_cases(tmp_path)
