import pytest

from apps.parsing.contracts import TransactionDirection
from apps.parsing.direction import SourceCategory, resolve_direction, source_category


def test_bank_labels_map_to_economic_direction() -> None:
    withdrawal = resolve_direction(source_type="bank_transaction_list", labels=("출금",))
    deposit = resolve_direction(source_type="bank_transaction_list", labels=("입금",))

    assert withdrawal.direction is TransactionDirection.DEBIT
    assert deposit.direction is TransactionDirection.CREDIT
    assert withdrawal.source_label == "출금"
    assert withdrawal.blocks_automatic_confirmation is False


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("withdrawal", TransactionDirection.DEBIT),
        ("deposit", TransactionDirection.CREDIT),
        ("refund", TransactionDirection.CREDIT),
        ("인출", TransactionDirection.DEBIT),
        ("환불", TransactionDirection.CREDIT),
    ],
)
def test_english_and_korean_labels_agree(label: str, expected: TransactionDirection) -> None:
    assert resolve_direction(source_type="bank_transaction_list", labels=(label,)).direction is (
        expected
    )


def test_the_same_label_is_read_differently_per_source_type() -> None:
    purchase = resolve_direction(source_type="card_transaction_list", labels=("결제",))
    settlement = resolve_direction(source_type="credit_card_payment", labels=("결제",))

    assert purchase.direction is TransactionDirection.DEBIT
    assert purchase.is_settlement is False
    assert settlement.direction is TransactionDirection.CREDIT
    assert settlement.is_settlement is True


def test_bank_side_of_a_card_settlement_is_a_debit() -> None:
    bank = resolve_direction(source_type="bank_transaction_list", labels=("카드대금",))
    statement = resolve_direction(source_type="credit_card_statement", labels=("청구",))

    assert bank.direction is TransactionDirection.DEBIT
    assert bank.is_settlement is True
    assert statement.direction is TransactionDirection.CREDIT
    assert statement.is_settlement is True


def test_display_sign_is_reported_separately_from_direction() -> None:
    resolution = resolve_direction(
        source_type="bank_transaction_list", labels=("출금",), display_sign="-"
    )

    assert resolution.display_sign == "-"
    assert resolution.direction is TransactionDirection.DEBIT


def test_bank_signs_are_interpreted_when_no_label_is_visible() -> None:
    negative = resolve_direction(source_type="bank_transaction_list", display_sign="-")
    positive = resolve_direction(source_type="bank_transaction_list", display_sign="+")

    assert negative.direction is TransactionDirection.DEBIT
    assert positive.direction is TransactionDirection.CREDIT
    assert negative.confidence < 0.95


def test_card_signs_are_interpreted_separately_from_bank_signs() -> None:
    card = resolve_direction(source_type="card_transaction_list", display_sign="-")
    unsigned = resolve_direction(source_type="card_transaction_list")

    # A negative amount on a card list is a cancellation, not a purchase.
    assert card.direction is TransactionDirection.CREDIT
    assert unsigned.direction is TransactionDirection.DEBIT


def test_debit_and_credit_card_sources_are_distinguished() -> None:
    assert source_category("card_transaction_list", instrument_type="debit_card") is (
        SourceCategory.DEBIT_CARD
    )
    assert source_category("card_transaction_list", instrument_type="credit_card") is (
        SourceCategory.CREDIT_CARD
    )
    assert source_category("card_transaction_list") is SourceCategory.CREDIT_CARD
    assert source_category("bank_transaction_detail") is SourceCategory.BANK
    assert source_category("unknown") is SourceCategory.UNKNOWN


def test_unknown_direction_blocks_automatic_confirmation() -> None:
    unlabelled = resolve_direction(source_type="unknown")
    transfer = resolve_direction(source_type="bank_transaction_list", labels=("이체",))

    assert unlabelled.direction is TransactionDirection.UNKNOWN
    assert unlabelled.blocks_automatic_confirmation is True
    assert transfer.direction is TransactionDirection.UNKNOWN
    assert transfer.source_label == "이체"
    assert transfer.blocks_automatic_confirmation is True


def test_signed_transfers_resolve_even_though_the_label_is_ambivalent() -> None:
    resolution = resolve_direction(
        source_type="bank_transaction_list", labels=("이체",), display_sign="-"
    )

    assert resolution.direction is TransactionDirection.DEBIT
    assert resolution.source_label == "이체"


def test_resolution_reasons_explain_the_decision() -> None:
    resolution = resolve_direction(source_type="card_transaction_list", labels=("승인",))

    assert "source_category=credit_card" in resolution.reasons
    assert any(reason.startswith("outflow_label=") for reason in resolution.reasons)
