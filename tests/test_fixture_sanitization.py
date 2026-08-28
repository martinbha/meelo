"""Tests for the committed-fixture sanitization guard."""

from pathlib import Path

from scripts.check_fixture_sanitization import find_unsanitized_identifiers, fixture_paths


def test_plausible_real_account_identifier_is_rejected(tmp_path: Path) -> None:
    fixture = tmp_path / "unsafe.json"
    fixture.write_text('{"account": "110-123-456789"}', encoding="utf-8")

    assert find_unsanitized_identifiers(fixture) == ["110-123-456789"]


def test_masked_identifier_is_allowed(tmp_path: Path) -> None:
    fixture = tmp_path / "safe.json"
    fixture.write_text('{"account": "110-***-4567"}', encoding="utf-8")

    assert find_unsanitized_identifiers(fixture) == []


def test_committed_fixtures_pass_sanitization_check() -> None:
    findings = {
        path: identifiers
        for path in fixture_paths()
        if (identifiers := find_unsanitized_identifiers(path))
    }

    assert findings == {}
