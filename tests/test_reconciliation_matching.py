from datetime import date
from typing import Any

import pytest

from apps.observations.models import ImportedObservation
from apps.reconciliation.duplicates import ObservationFacts
from apps.reconciliation.matching import (
    STRONG_MATCH_SCORE,
    classify_charge,
    is_card_issuer_counterparty,
    match_credit_card_settlement,
    match_debit_card_to_bank,
    match_internal_transfer,
    match_refund_to_purchase,
    summarize_settlement,
)

DEBIT = ImportedObservation.Direction.DEBIT
CREDIT = ImportedObservation.Direction.CREDIT


def facts(**overrides: Any) -> ObservationFacts:
    values: dict[str, Any] = {
        "observation_id": "row-1",
        "user_id": 1,
        "occurred_at": date(2026, 8, 15),
        "amount_minor": 42_900,
        "currency": "KRW",
        "direction": DEBIT,
        "merchant": "이마트 성수점",
        "approval_code": "",
        "balance_after_minor": None,
        "instrument_id": None,
        "account_id": None,
        "source_type": "",
        "source_document_id": "doc-1",
    }
    values.update(overrides)
    return ObservationFacts(**values)


# ---------------------------------------------------------------------------
# Debit-card and bank match (#73, specification 17.1)
# ---------------------------------------------------------------------------


def test_a_mapped_card_and_bank_row_match_strongly() -> None:
    card = facts(observation_id="card", instrument_id="card-1", source_type="card_transaction_list")
    bank = facts(
        observation_id="bank",
        account_id="account-1",
        occurred_at=date(2026, 8, 16),
        source_type="bank_transaction_list",
    )

    proposal = match_debit_card_to_bank(card, bank, card_account_id="account-1")

    assert proposal is not None
    assert proposal.match_type == "debit_card_bank_match"
    assert "card_mapped_to_account" in proposal.features
    assert proposal.score >= STRONG_MATCH_SCORE
    # Both observations are named; neither is replaced by the other.
    assert proposal.left_observation_id == "card"
    assert proposal.right_observation_id == "bank"


def test_an_unmapped_card_still_proposes_but_needs_review() -> None:
    card = facts(observation_id="card", merchant="이마트")
    bank = facts(observation_id="bank", merchant="완전히 다른 이름")

    proposal = match_debit_card_to_bank(card, bank)

    assert proposal is not None
    assert proposal.needs_review is True
    assert "card_mapped_to_account" not in proposal.features


def test_different_amounts_or_distant_dates_do_not_match() -> None:
    card = facts(observation_id="card")

    assert match_debit_card_to_bank(card, facts(observation_id="bank", amount_minor=1)) is None
    assert (
        match_debit_card_to_bank(card, facts(observation_id="bank", occurred_at=date(2026, 9, 30)))
        is None
    )
    assert match_debit_card_to_bank(card, facts(observation_id="bank", direction=CREDIT)) is None


def test_rows_from_different_users_never_match() -> None:
    assert match_debit_card_to_bank(facts(), facts(observation_id="other", user_id=2)) is None


# ---------------------------------------------------------------------------
# Credit-card settlement (#74, specification 17.2)
# ---------------------------------------------------------------------------


def test_a_bank_withdrawal_to_an_issuer_is_a_settlement_not_spending() -> None:
    withdrawal = facts(
        observation_id="bank",
        merchant="현대카드",
        amount_minor=382_400,
        account_id="settlement-account",
    )
    statement = facts(
        observation_id="statement",
        merchant="현대카드 청구금액",
        amount_minor=382_400,
        direction=CREDIT,
        occurred_at=date(2026, 8, 25),
    )

    proposal = match_credit_card_settlement(
        withdrawal, statement, settlement_account_id="settlement-account"
    )

    assert proposal is not None
    assert proposal.match_type == "credit_card_payment"
    assert "issuer_counterparty" in proposal.features
    assert "configured_settlement_account" in proposal.features
    assert "amount_matches_statement" in proposal.features


def test_a_partial_payment_is_recognised_as_partial() -> None:
    withdrawal = facts(observation_id="bank", merchant="삼성카드", amount_minor=100_000)
    statement = facts(observation_id="statement", amount_minor=382_400, direction=CREDIT)

    proposal = match_credit_card_settlement(withdrawal, statement, statement_balance_minor=382_400)

    assert proposal is not None
    assert "partial_payment" in proposal.features


def test_an_unmapped_issuer_produces_no_settlement_match() -> None:
    withdrawal = facts(observation_id="bank", merchant="동네 반찬가게", amount_minor=10_000)
    statement = facts(observation_id="statement", amount_minor=382_400, direction=CREDIT)

    # Nothing identifies this as a card payment, so it stays an ordinary,
    # reviewable withdrawal rather than being reclassified.
    assert match_credit_card_settlement(withdrawal, statement) is None


def test_a_credit_row_is_never_read_as_a_settlement_payment() -> None:
    inflow = facts(observation_id="bank", merchant="현대카드", direction=CREDIT)
    statement = facts(observation_id="statement", direction=CREDIT)

    assert match_credit_card_settlement(inflow, statement) is None


def test_issuer_counterparties_are_recognised() -> None:
    assert is_card_issuer_counterparty("현대카드") is True
    assert is_card_issuer_counterparty("Samsung Card") is True
    assert is_card_issuer_counterparty("동네 빵집") is False


# ---------------------------------------------------------------------------
# Partial, multiple, and irregular settlements (#75)
# ---------------------------------------------------------------------------


def test_multiple_payments_reconcile_to_one_statement_without_double_counting() -> None:
    coverage = summarize_settlement(statement_minor=382_400, payments=[200_000, 182_400])

    assert coverage.paid_minor == 382_400
    assert coverage.outstanding_minor == 0
    assert coverage.is_settled is True
    assert coverage.is_partially_settled is False


def test_a_partial_payment_leaves_the_remainder_outstanding() -> None:
    coverage = summarize_settlement(statement_minor=382_400, payments=[100_000])

    assert coverage.outstanding_minor == 282_400
    assert coverage.is_partially_settled is True
    assert coverage.is_settled is False


def test_a_refund_before_payment_reduces_what_is_owed() -> None:
    coverage = summarize_settlement(statement_minor=382_400, payments=[300_000], refunds=[82_400])

    assert coverage.refunded_minor == 82_400
    assert coverage.outstanding_minor == 0
    assert coverage.is_settled is True


def test_overpayment_is_visible_rather_than_clamped() -> None:
    coverage = summarize_settlement(statement_minor=100_000, payments=[150_000])

    assert coverage.outstanding_minor == -50_000
    assert coverage.is_overpaid is True


def test_fees_and_interest_stay_reportable_expenses() -> None:
    coverage = summarize_settlement(
        statement_minor=382_400,
        payments=[382_400],
        fees=[12_000],
        interest=[3_500],
    )

    # The settlement itself is not spending; the fee and the interest are.
    assert coverage.is_settled is True
    assert coverage.reportable_expense_minor == 15_500


def test_negative_settlement_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        summarize_settlement(statement_minor=-1)
    with pytest.raises(ValueError):
        summarize_settlement(statement_minor=100, payments=[-1])


def test_fees_and_interest_are_classified_apart_from_purchases() -> None:
    assert classify_charge("연회비") == "fee"
    assert classify_charge("할부수수료") == "interest"
    assert classify_charge("이자") == "interest"
    assert classify_charge("스타벅스") is None


# ---------------------------------------------------------------------------
# Internal transfers and refunds (specification 17.3, 17.4)
# ---------------------------------------------------------------------------


def test_opposite_rows_on_two_owned_accounts_propose_a_transfer() -> None:
    outgoing = facts(observation_id="out", account_id="account-1")
    incoming = facts(observation_id="in", account_id="account-2", direction=CREDIT)

    proposal = match_internal_transfer(outgoing, incoming)

    assert proposal is not None
    assert proposal.match_type == "internal_transfer"
    assert "both_accounts_owned" in proposal.features


def test_a_transfer_needs_two_different_owned_accounts() -> None:
    outgoing = facts(observation_id="out", account_id="account-1")

    same_account = facts(observation_id="in", account_id="account-1", direction=CREDIT)
    unmapped = facts(observation_id="in", account_id=None, direction=CREDIT)

    assert match_internal_transfer(outgoing, same_account) is None
    assert match_internal_transfer(outgoing, unmapped) is None


def test_a_refund_is_matched_to_the_purchase_it_reverses() -> None:
    purchase = facts(observation_id="purchase", account_id="account-1")
    refund = facts(
        observation_id="refund",
        direction=CREDIT,
        occurred_at=date(2026, 8, 20),
        account_id="account-1",
    )

    proposal = match_refund_to_purchase(refund, purchase)

    assert proposal is not None
    assert proposal.match_type == "refund_match"
    assert "exact_amount" in proposal.features
    assert "similar_merchant" in proposal.features


def test_a_refund_dated_before_its_purchase_is_not_matched() -> None:
    purchase = facts(observation_id="purchase", occurred_at=date(2026, 8, 20))
    refund = facts(observation_id="refund", direction=CREDIT, occurred_at=date(2026, 8, 15))

    assert match_refund_to_purchase(refund, purchase) is None


def test_a_partial_refund_is_recognised() -> None:
    purchase = facts(observation_id="purchase", account_id="account-1")
    refund = facts(
        observation_id="refund",
        direction=CREDIT,
        amount_minor=10_000,
        occurred_at=date(2026, 8, 20),
        account_id="account-1",
    )

    proposal = match_refund_to_purchase(refund, purchase)

    assert proposal is not None
    assert "partial_refund" in proposal.features
