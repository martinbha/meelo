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
def test_korean_and_english_normalization_is_deterministic(
    value: str, expected: str
) -> None:
    assert normalize_ocr_text(value) == expected
    assert normalize_ocr_text(value) == normalize_ocr_text(value)
