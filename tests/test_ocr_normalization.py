import json
from pathlib import Path

import pytest

from apps.ocr.normalization import normalize_money_text, normalize_ocr_text


@pytest.mark.parametrize("value", ["₩42,900", "42,900원", "42 900 원", "４２，９００원"])
def test_korean_money_variants_share_one_representation(value: str) -> None:
    assert normalize_money_text(value) == "42900 KRW"
    assert normalize_ocr_text(value) == "42900 KRW"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("２０２６년 ８월 ５일", "2026-08-05"),
        ("2026. 8. 5", "2026-08-05"),
        ("  STARBUCKS　KOREA  ", "starbucks korea"),
        ("승인 : 완료", "승인: 완료"),
    ],
)
def test_korean_and_english_normalization_is_deterministic(value: str, expected: str) -> None:
    assert normalize_ocr_text(value) == expected
    assert normalize_ocr_text(value) == normalize_ocr_text(value)


def test_checked_in_normalization_corpus_is_idempotent() -> None:
    path = Path(__file__).parent / "fixtures" / "ocr" / "normalization-corpus.json"
    cases = json.loads(path.read_text(encoding="utf-8"))

    for case in cases:
        normalized = normalize_ocr_text(case["raw"])
        assert normalized == case["normalized"]
        assert normalize_ocr_text(normalized) == normalized


@pytest.mark.parametrize("value", ["1200", "-1200", "12.50", "B2B STORE", "S1 COFFEE"])
def test_normalization_never_changes_an_existing_numeric_value(value: str) -> None:
    original_digits = "".join(character for character in value if character.isdigit())
    normalized_digits = "".join(
        character for character in normalize_ocr_text(value) if character.isdigit()
    )
    assert normalized_digits == original_digits
