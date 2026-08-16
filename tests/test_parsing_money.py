import pytest

from apps.parsing.money import looks_like_money, parse_money


@pytest.mark.parametrize(
    ("text", "minor", "currency"),
    [
        ("42,900원", 42900, "KRW"),
        ("₩42,900", 42900, "KRW"),
        ("KRW 42,900", 42900, "KRW"),
        ("42900 KRW", 42900, "KRW"),
        ("42 900원", 42900, "KRW"),
        ("1,234,567원", 1234567, "KRW"),
        ("1.234.567원", 1234567, "KRW"),
        ("$10.25", 1025, "USD"),
        ("USD 10.25", 1025, "USD"),
        ("1,234.50 USD", 123450, "USD"),
        ("1.234,50 EUR", 123450, "EUR"),
    ],
)
def test_examples_normalize_to_correct_minor_units(text: str, minor: int, currency: str) -> None:
    candidate = parse_money(text)

    assert candidate is not None
    assert candidate.money is not None
    assert candidate.money.amount_minor == minor
    assert str(candidate.money.resolved_currency) == currency
    assert candidate.requires_review is False


def test_thousands_separators_are_not_read_as_decimals() -> None:
    krw = parse_money("1,234원")
    dotted = parse_money("1.234.567원")

    assert krw is not None and krw.money is not None
    assert krw.money.amount_minor == 1234
    assert dotted is not None and dotted.money is not None
    assert dotted.money.amount_minor == 1234567
    assert "thousands_grouping" in dotted.reasons


def test_source_sign_is_preserved_separately_from_the_amount() -> None:
    negative = parse_money("-42,900원")
    positive = parse_money("+42,900원")

    assert negative is not None and negative.money is not None
    assert negative.source_sign == "-"
    assert negative.money.amount_minor == 42900
    assert negative.signed_minor == -42900
    assert positive is not None
    assert positive.source_sign == "+"
    assert positive.signed_minor == 42900


def test_source_label_is_preserved_separately_from_direction() -> None:
    candidate = parse_money("출금 42,900원")

    assert candidate is not None
    assert candidate.source_label == "출금"
    assert candidate.money is not None
    assert candidate.money.amount_minor == 42900


@pytest.mark.parametrize(
    "text",
    [
        "1.234 USD",  # grouping or a two-decimal fraction
        "12,50원",  # a fraction that KRW cannot express
        "10.5 USD",  # wrong number of fractional digits
        "1,23,456원",  # broken grouping
    ],
)
def test_ambiguous_amounts_are_flagged_rather_than_guessed(text: str) -> None:
    candidate = parse_money(text)

    assert candidate is not None
    assert candidate.money is None
    assert candidate.ambiguous is True
    assert candidate.requires_review is True
    assert any(reason.startswith("ambiguous") for reason in candidate.reasons)


def test_unambiguous_single_group_krw_is_accepted() -> None:
    candidate = parse_money("1.234원")

    assert candidate is not None
    assert candidate.money is not None
    assert candidate.money.amount_minor == 1234


def test_ocr_digit_noise_is_repaired_with_reduced_confidence() -> None:
    candidate = parse_money("4Z,9OO원")

    assert candidate is not None
    assert candidate.money is not None
    assert candidate.money.amount_minor == 42900
    assert "ocr_digit_repair" in candidate.reasons
    assert candidate.confidence < 0.9


def test_default_currency_is_recorded_when_no_marker_is_visible() -> None:
    candidate = parse_money("42,900")

    assert candidate is not None
    assert candidate.money is not None
    assert str(candidate.money.resolved_currency) == "KRW"
    assert "currency_defaulted" in candidate.reasons


def test_default_currency_can_be_overridden_per_document() -> None:
    candidate = parse_money("1,234.50", default_currency="USD")

    assert candidate is not None
    assert candidate.money is not None
    assert candidate.money.amount_minor == 123450


def test_digit_repair_never_rewrites_surrounding_text() -> None:
    # "SOS" would repair to "505", but the token has no digits to anchor it.
    assert parse_money("SOS") is None
    # Letters that survive the repair keep the token out of the amount path.
    assert parse_money("gate 5b") is None


def test_non_amount_text_is_not_parsed() -> None:
    assert parse_money("스타벅스") is None
    assert parse_money("") is None
    assert parse_money("입금") is None


def test_tokens_carrying_more_than_an_amount_are_not_parsed() -> None:
    assert parse_money("스타벅스 4,200원") is None
    assert parse_money("승인번호 12345678") is None


def test_looks_like_money_ignores_bare_digit_runs() -> None:
    assert looks_like_money("42,900원")
    assert looks_like_money("$10.25")
    assert looks_like_money("-1200")
    assert not looks_like_money("1234")
    assert not looks_like_money("스타벅스")
