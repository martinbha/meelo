"""Regression coverage for the institution-specific screenshot parsers."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from apps.ocr.contracts import BoundingBox
from apps.ocr.normalization import normalize_ocr_text
from apps.parsing.contracts import (
    DocumentMetadata,
    NormalizedToken,
    TransactionDirection,
)
from apps.parsing.fixture_harness import (
    ParserFixtureCase,
    load_parser_fixtures,
    run_parser_fixture_suite,
    summarize,
)
from apps.parsing.generic import GenericTransactionListParser
from apps.parsing.institutions import (
    INSTITUTION_PARSER_CLASSES,
    HyundaiCardParser,
    KakaoBankParser,
    ShinhanBankParser,
    TossBankParser,
    build_institution_parsers,
)
from apps.parsing.institutions.base import COLUMN_DIRECTION_CONFIDENCE
from apps.parsing.registry import ParserRegistry

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parsers"

#: Fixtures are sanitized and exact, so every tracked metric must be perfect.
#: Loosening a threshold is a deliberate decision, not an accident.
REQUIRED_FIELD_ACCURACY = 1.0
MAXIMUM_MISSED_RATE = 0.0
MAXIMUM_FALSE_RATE = 0.0


def build_registry() -> ParserRegistry:
    registry = ParserRegistry(generic_parser=GenericTransactionListParser())
    for parser in build_institution_parsers():
        registry.register(parser)
    return registry


def token(text: str, left: int, top: int, *, sequence: int, width: int = 120) -> NormalizedToken:
    return NormalizedToken(
        normalize_ocr_text(text),
        0.95,
        BoundingBox(left, top, left + width, top + 30),
        sequence,
        ("fixture",),
    )


def tokens_from(rows: Sequence[Sequence[tuple[str, int]]]) -> tuple[NormalizedToken, ...]:
    """Build tokens from ``(text, left)`` pairs, one sequence per screen row."""

    built: list[NormalizedToken] = []
    for row_index, row in enumerate(rows):
        for text, left in row:
            built.append(token(text, left, 100 + row_index * 80, sequence=len(built)))
    return tuple(built)


def document(source_type: str = "bank_transaction_list", **kwargs: object) -> DocumentMetadata:
    defaults: dict[str, object] = {
        "uploaded_at": datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        "time_zone": "Asia/Seoul",
    }
    defaults.update(kwargs)
    return DocumentMetadata(source_type, 1080, 1920, **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixture regression suite (specification 31.3)
# ---------------------------------------------------------------------------


def all_cases() -> tuple[ParserFixtureCase, ...]:
    return load_parser_fixtures(FIXTURE_ROOT)


def test_every_institution_ships_at_least_one_fixture() -> None:
    covered = {case.parser for case in all_cases()}
    expected = {parser_class.profile.name for parser_class in INSTITUTION_PARSER_CLASSES}

    assert covered == expected


@pytest.mark.parametrize("case", all_cases(), ids=lambda case: case.name)
def test_fixtures_meet_accuracy_targets(case: ParserFixtureCase) -> None:
    metrics = run_parser_fixture_suite((case,), registry=build_registry())[0]

    assert metrics.selected_parser == case.parser, summarize((metrics,))
    assert metrics.amount_accuracy >= REQUIRED_FIELD_ACCURACY, metrics.mismatches
    assert metrics.date_accuracy >= REQUIRED_FIELD_ACCURACY, metrics.mismatches
    assert metrics.merchant_accuracy >= REQUIRED_FIELD_ACCURACY, metrics.mismatches
    assert metrics.direction_accuracy >= REQUIRED_FIELD_ACCURACY, metrics.mismatches
    assert metrics.metadata_accuracy >= REQUIRED_FIELD_ACCURACY, metrics.mismatches
    assert metrics.missed_rate <= MAXIMUM_MISSED_RATE, metrics.mismatches
    assert metrics.false_rate <= MAXIMUM_FALSE_RATE, metrics.mismatches
    assert metrics.is_clean, summarize((metrics,))


@pytest.mark.parametrize("case", all_cases(), ids=lambda case: case.name)
def test_fixtures_report_the_expected_source_type_and_support(
    case: ParserFixtureCase,
) -> None:
    selection = build_registry().parse(case.document, case.tokens)

    assert selection.support.score >= case.minimum_confidence
    assert selection.support.score <= case.maximum_confidence
    if case.expected_source_type is not None:
        assert selection.support.detected_source_type == case.expected_source_type


@pytest.mark.parametrize("case", all_cases(), ids=lambda case: case.name)
def test_parser_metadata_is_stored_with_every_observation(case: ParserFixtureCase) -> None:
    selection = build_registry().parse(case.document, case.tokens)

    assert selection.observations
    for observation in selection.observations:
        assert observation.parser_name == case.parser
        assert observation.parser_version == selection.metadata.version
        assert observation.parser_support_score == selection.support.score
        assert observation.output_version >= 1


# ---------------------------------------------------------------------------
# Source detection and fallback
# ---------------------------------------------------------------------------


def test_each_parser_claims_only_its_own_institution() -> None:
    registry = build_registry()
    toss_tokens = tokens_from(((("토스뱅크", 40), ("거래내역", 240)),))

    for parser in build_institution_parsers():
        support = parser.supports(document(), toss_tokens)
        if parser.metadata.name == "toss_bank":
            assert support.score > 0.5
        else:
            assert support.score == 0.0

    assert registry.select(document(), toss_tokens)[0].metadata.name == "toss_bank"


def test_unknown_institutions_fall_back_to_the_generic_parser() -> None:
    tokens = tokens_from(((("2026-08-15", 40), ("cafe", 240), ("debit", 440), ("4200 KRW", 640)),))

    parser, support = build_registry().select(document(), tokens)

    assert parser.metadata.name == "generic"
    assert support.score < 0.5


def test_an_unsupported_layout_falls_back_without_inventing_rows() -> None:
    # Toss is recognised, but nothing on the screen looks like a transaction.
    tokens = tokens_from(
        (
            (("토스뱅크", 40), ("서비스 점검 안내", 240)),
            (("잠시 후 다시 시도해 주세요", 40),),
        )
    )

    observations = TossBankParser().parse(document(), tokens)

    assert all(item.amount is None for item in observations)
    assert all(
        item.confidence_factors.get("institution_fallback") == "toss_bank" for item in observations
    )
    assert all(item.confidence_factors["requires_review"] is True for item in observations)


def test_a_manual_override_selects_a_parser_the_markers_missed() -> None:
    tokens = tokens_from(
        ((("2026.08.15", 40), ("스타벅스", 240), ("출금", 440), ("4,200원", 640)),)
    )
    override = document("bank_transaction_list", manual_source_override="kakao_bank")

    selection = build_registry().parse(override, tokens)

    assert selection.metadata.name == "kakao_bank"
    assert selection.observations[0].amount_minor == 4200


def test_an_institution_hint_supports_a_parser_without_visible_markers() -> None:
    tokens = tokens_from(
        ((("2026.08.15", 40), ("스타벅스", 240), ("출금", 440), ("4,200원", 640)),)
    )

    support = KakaoBankParser().supports(
        document("bank_transaction_list", institution_hint="카카오뱅크"), tokens
    )

    assert support.score > 0.5


# ---------------------------------------------------------------------------
# Row-level behaviour
# ---------------------------------------------------------------------------


def test_a_row_that_cannot_be_read_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TossBankParser()
    tokens = tokens_from(
        (
            (("토스뱅크", 40), ("거래내역", 240)),
            (("2026.08.15", 40), ("스타벅스", 240), ("출금", 440), ("4,200원", 640)),
        )
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("row extraction failed")

    monkeypatch.setattr(TossBankParser, "extract_row", explode)
    observations = parser.parse(document(), tokens)

    assert observations
    unreadable = [item for item in observations if "parser_error" in item.confidence_factors]
    assert unreadable
    assert all(item.direction is TransactionDirection.UNKNOWN for item in unreadable)
    assert all(item.blocks_automatic_confirmation for item in unreadable)


def test_column_position_decides_direction_when_no_label_is_printed() -> None:
    tokens = tokens_from(
        (
            (("신한은행", 40), ("거래내역", 240)),
            (("거래일자", 40), ("적요", 240), ("출금", 560), ("입금", 760)),
            (("2026.08.10", 40), ("이마트", 240), ("42,900", 560)),
            (("2026.08.09", 40), ("급여", 240), ("2,500,000", 760)),
        )
    )

    observations = ShinhanBankParser().parse(document(), tokens)

    assert [item.direction for item in observations] == [
        TransactionDirection.DEBIT,
        TransactionDirection.CREDIT,
    ]
    # Neither row printed a label or a sign, so the column is the only evidence.
    assert all(item.direction_label is None for item in observations)
    assert all(item.display_sign == "" for item in observations)
    assert all(
        float(item.confidence_factors["direction_confidence"]) == COLUMN_DIRECTION_CONFIDENCE
        for item in observations
    )


def test_inferred_years_are_marked_for_review() -> None:
    tokens = tokens_from(
        (
            (("카카오뱅크", 40), ("거래내역", 240)),
            (("08.14", 40), ("gs25", 240), ("출금", 440), ("3,500원", 640)),
        )
    )

    observation = KakaoBankParser().parse(document(), tokens)[0]

    assert observation.occurred_on == date(2026, 8, 14)
    assert observation.confidence_factors["date_inference"] == "upload_year"
    assert observation.confidence_factors["requires_review"] is True


def test_balance_mismatches_are_surfaced_without_changing_the_amount() -> None:
    tokens = tokens_from(
        (
            (("카카오뱅크", 40), ("거래내역", 240)),
            (
                ("2026.08.14", 40),
                ("배달의민족", 240),
                ("출금", 440),
                ("23,000원", 640),
                ("잔액", 840),
                ("1,217,500원", 940),
            ),
            (
                ("2026.08.13", 40),
                ("이자", 240),
                ("입금", 440),
                ("1,200원", 640),
                ("잔액", 840),
                ("1,243,500원", 940),
            ),
        )
    )

    observations = KakaoBankParser().parse(document(), tokens)

    mismatched = observations[0]
    assert mismatched.amount_minor == 23000
    assert mismatched.balance_after == 1217500
    assert mismatched.confidence_factors["balance_status"] == "invalid"
    assert "balance_after" in mismatched.ambiguous_fields
    assert mismatched.confidence_factors["requires_review"] is True


def test_a_valid_balance_chain_raises_confidence() -> None:
    tokens = tokens_from(
        (
            (("카카오뱅크", 40), ("거래내역", 240)),
            (
                ("2026.08.14", 40),
                ("배달의민족", 240),
                ("출금", 440),
                ("23,000원", 640),
                ("잔액", 840),
                ("1,220,500원", 940),
            ),
            (
                ("2026.08.13", 40),
                ("이자", 240),
                ("입금", 440),
                ("1,200원", 640),
                ("잔액", 840),
                ("1,243,500원", 940),
            ),
        )
    )

    observation = KakaoBankParser().parse(document(), tokens)[0]

    assert observation.confidence_factors["balance_status"] == "valid"
    assert float(observation.confidence_factors["balance_confidence_delta"]) > 0
    assert observation.ambiguous_fields == frozenset()


def test_card_statement_payments_are_not_emitted_as_purchases() -> None:
    tokens = tokens_from(
        (
            (("삼성카드", 40), ("이용대금명세서", 240)),
            (("2026.08.25", 40), ("청구금액", 240), ("1,204,300원", 740)),
            (("2026.08.20", 40), ("다이소", 240), ("일시불", 540), ("8,900원", 740)),
        )
    )

    settlement, purchase = (
        build_registry()
        .parse(document("credit_card_statement", instrument_type="credit_card"), tokens)
        .observations
    )

    assert settlement.is_settlement is True
    assert settlement.direction is TransactionDirection.CREDIT
    assert purchase.is_settlement is False
    assert purchase.direction is TransactionDirection.DEBIT


def test_card_installment_and_approval_metadata_is_preserved() -> None:
    tokens = tokens_from(
        (
            (("현대카드", 40), ("승인상세", 240)),
            (("****-1234", 40),),
            (("2026.08.14", 40), ("이케아", 240), ("할부 6개월", 480), ("480,000원", 720)),
            (("승인번호", 40), ("12345678", 240)),
        )
    )

    observation = HyundaiCardParser().parse(
        document("card_transaction_detail", instrument_type="credit_card"), tokens
    )[0]

    assert observation.installment_months == 6
    assert observation.approval_code == "12345678"
    assert observation.instrument_suffix == "1234"
    assert observation.amount_minor == 480000
    assert observation.direction is TransactionDirection.DEBIT
