"""Resolving several views of one real financial event.

A debit-card purchase shows up twice — once in the card app, once in the bank
app — and is one purchase. A bank withdrawal to a card issuer is not spending,
it is the settlement of money already spent. Counting either of those twice is
the specific mistake this module exists to prevent (specification 2.3, 17).

Every function here proposes; none of them decide. Weak evidence produces a
candidate for review rather than a link.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rapidfuzz.fuzz import ratio

from apps.observations.models import ImportedObservation

from .duplicates import ObservationFacts

#: A card purchase and its bank counterpart usually post within a few days.
CARD_BANK_WINDOW = timedelta(days=3)
#: Card settlements land within roughly a billing cycle of the statement.
SETTLEMENT_WINDOW = timedelta(days=45)
#: Two sides of an internal transfer are recorded within a day of each other.
TRANSFER_WINDOW = timedelta(days=1)
#: A refund can follow its purchase by a long time.
REFUND_WINDOW = timedelta(days=120)

#: Score at or above which a match may be linked without review.
STRONG_MATCH_SCORE = 85
#: Score below which a pair is not even worth showing. Set so that the core
#: signal alone — same amount, compatible direction, nearby date — still
#: surfaces as a reviewable candidate when the card is not yet mapped to its
#: account. Callers pair card-source rows against bank-source rows, so this
#: does not turn two similar purchases on one statement into a match.
MINIMUM_MATCH_SCORE = 40

MERCHANT_SIMILARITY_THRESHOLD = 85.0

#: Counterparty names that identify a Korean card issuer being paid.
CARD_ISSUER_MARKERS = (
    "카드",
    "현대카드",
    "삼성카드",
    "신한카드",
    "국민카드",
    "롯데카드",
    "비씨카드",
    "하나카드",
    "card",
)

#: Labels that mark a fee or interest charge, which stay separate expenses.
FEE_MARKERS = ("수수료", "연회비", "fee", "charge")
INTEREST_MARKERS = ("이자", "이자율", "interest", "할부수수료")


@dataclass(frozen=True, slots=True)
class MatchProposal:
    """One proposed relationship, with the evidence behind it."""

    left_observation_id: Any
    right_observation_id: Any
    match_type: str
    score: int
    features: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_review(self) -> bool:
        """Weak matches are never linked automatically."""

        return self.score < STRONG_MATCH_SCORE


def _similar_merchant(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return float(ratio(left.casefold(), right.casefold())) >= MERCHANT_SIMILARITY_THRESHOLD


def _within(left: date | None, right: date | None, window: timedelta) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= window


def _same_amount(left: ObservationFacts, right: ObservationFacts) -> bool:
    return (
        left.amount_minor is not None
        and left.amount_minor == right.amount_minor
        and left.currency == right.currency
    )


def _contains(text: str, markers: Sequence[str]) -> bool:
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in markers)


def is_card_issuer_counterparty(text: str) -> bool:
    """Whether a counterparty name looks like a card issuer being paid."""

    return _contains(text, CARD_ISSUER_MARKERS)


def classify_charge(merchant: str) -> str | None:
    """Tell a fee or interest charge from ordinary spending.

    Fees and interest are real expenses and must stay reportable as such, so
    they are never folded into the settlement they arrive with.
    """

    if _contains(merchant, INTEREST_MARKERS):
        return "interest"
    if _contains(merchant, FEE_MARKERS):
        return "fee"
    return None


def match_debit_card_to_bank(
    card: ObservationFacts,
    bank: ObservationFacts,
    *,
    card_account_id: Any = None,
) -> MatchProposal | None:
    """Pair a card-app purchase with the bank row for the same purchase.

    ``card_account_id`` is the bank account the card is mapped to; without that
    mapping the pair can still be proposed, but it scores lower because the
    strongest signal is missing.
    """

    if card.user_id != bank.user_id or card.observation_id == bank.observation_id:
        return None
    if not _same_amount(card, bank):
        return None
    if card.direction != bank.direction:
        return None
    if not _within(card.occurred_at, bank.occurred_at, CARD_BANK_WINDOW):
        return None

    score = 40
    features = ["exact_amount", "compatible_direction", "nearby_date"]
    if card_account_id is not None and bank.account_id == card_account_id:
        score += 30
        features.append("card_mapped_to_account")
    if _similar_merchant(card.merchant, bank.merchant):
        score += 15
        features.append("similar_merchant")
    if card.approval_code and card.approval_code == bank.approval_code:
        score += 25
        features.append("same_approval_code")
    if card.occurred_at == bank.occurred_at:
        score += 5
        features.append("same_date")

    if score < MINIMUM_MATCH_SCORE:
        return None
    return MatchProposal(
        card.observation_id,
        bank.observation_id,
        "debit_card_bank_match",
        min(100, score),
        tuple(features),
    )


def match_credit_card_settlement(
    bank_withdrawal: ObservationFacts,
    statement: ObservationFacts,
    *,
    settlement_account_id: Any = None,
    statement_balance_minor: int | None = None,
) -> MatchProposal | None:
    """Recognise a bank withdrawal that settles a card balance.

    A settlement is not spending: the money it moves was already counted when
    the purchases were made. Classifying it as an expense would double-count
    every card purchase in the month.
    """

    if bank_withdrawal.user_id != statement.user_id:
        return None
    if bank_withdrawal.observation_id == statement.observation_id:
        return None
    if bank_withdrawal.direction != ImportedObservation.Direction.DEBIT:
        return None
    if not _within(bank_withdrawal.occurred_at, statement.occurred_at, SETTLEMENT_WINDOW):
        return None

    score = 20
    features = ["settlement_window"]
    if is_card_issuer_counterparty(bank_withdrawal.merchant):
        score += 30
        features.append("issuer_counterparty")
    if settlement_account_id is not None and bank_withdrawal.account_id == settlement_account_id:
        score += 20
        features.append("configured_settlement_account")
    if _same_amount(bank_withdrawal, statement):
        score += 25
        features.append("amount_matches_statement")
    elif (
        statement_balance_minor is not None
        and bank_withdrawal.amount_minor is not None
        and bank_withdrawal.amount_minor == statement_balance_minor
    ):
        score += 20
        features.append("amount_matches_outstanding_balance")
    elif (
        statement_balance_minor is not None
        and bank_withdrawal.amount_minor is not None
        and 0 < bank_withdrawal.amount_minor < statement_balance_minor
    ):
        score += 10
        features.append("partial_payment")

    if score < MINIMUM_MATCH_SCORE:
        return None
    return MatchProposal(
        bank_withdrawal.observation_id,
        statement.observation_id,
        "credit_card_payment",
        min(100, score),
        tuple(features),
    )


@dataclass(frozen=True, slots=True)
class SettlementCoverage:
    """How a set of payments relates to one statement's balance."""

    statement_minor: int
    paid_minor: int
    refunded_minor: int
    fee_minor: int
    interest_minor: int

    @property
    def outstanding_minor(self) -> int:
        """What is still owed after payments and refunds are applied."""

        return self.statement_minor - self.paid_minor - self.refunded_minor

    @property
    def is_settled(self) -> bool:
        return self.outstanding_minor <= 0

    @property
    def is_partially_settled(self) -> bool:
        return 0 < self.outstanding_minor < self.statement_minor

    @property
    def is_overpaid(self) -> bool:
        return self.outstanding_minor < 0

    @property
    def reportable_expense_minor(self) -> int:
        """Fees and interest are spending; the settlement itself is not."""

        return self.fee_minor + self.interest_minor


def summarize_settlement(
    *,
    statement_minor: int,
    payments: Sequence[int] = (),
    refunds: Sequence[int] = (),
    fees: Sequence[int] = (),
    interest: Sequence[int] = (),
) -> SettlementCoverage:
    """Reconcile several payments against one statement.

    Real settlement is untidy: two payments in one month, a refund landing
    before the payment, a late fee added on top. Summing the payments once here
    is what keeps multiple payments from double-counting against one statement.
    """

    if statement_minor < 0:
        raise ValueError("A statement balance cannot be negative.")
    for label, values in (
        ("payments", payments),
        ("refunds", refunds),
        ("fees", fees),
        ("interest", interest),
    ):
        if any(value < 0 for value in values):
            raise ValueError(f"Individual {label} amounts cannot be negative.")
    return SettlementCoverage(
        statement_minor=statement_minor,
        paid_minor=sum(payments),
        refunded_minor=sum(refunds),
        fee_minor=sum(fees),
        interest_minor=sum(interest),
    )


def match_internal_transfer(
    outgoing: ObservationFacts, incoming: ObservationFacts
) -> MatchProposal | None:
    """Propose a transfer when two owned accounts show opposite sides of one move."""

    if outgoing.user_id != incoming.user_id:
        return None
    if outgoing.observation_id == incoming.observation_id:
        return None
    if outgoing.direction != ImportedObservation.Direction.DEBIT:
        return None
    if incoming.direction != ImportedObservation.Direction.CREDIT:
        return None
    if not _same_amount(outgoing, incoming):
        return None
    if not _within(outgoing.occurred_at, incoming.occurred_at, TRANSFER_WINDOW):
        return None
    # Both sides must be owned accounts, and they must be different ones:
    # a row cannot transfer to itself.
    if outgoing.account_id is None or incoming.account_id is None:
        return None
    if outgoing.account_id == incoming.account_id:
        return None

    score = 70
    features = ["opposite_directions", "exact_amount", "nearby_date", "both_accounts_owned"]
    if outgoing.occurred_at == incoming.occurred_at:
        score += 15
        features.append("same_date")
    return MatchProposal(
        outgoing.observation_id,
        incoming.observation_id,
        "internal_transfer",
        min(100, score),
        tuple(features),
    )


def match_refund_to_purchase(
    refund: ObservationFacts, purchase: ObservationFacts
) -> MatchProposal | None:
    """Connect a refund to the purchase it reverses, so spending drops."""

    if refund.user_id != purchase.user_id:
        return None
    if refund.observation_id == purchase.observation_id:
        return None
    if refund.direction != ImportedObservation.Direction.CREDIT:
        return None
    if purchase.direction != ImportedObservation.Direction.DEBIT:
        return None
    if refund.occurred_at is None or purchase.occurred_at is None:
        return None
    # A refund follows its purchase; the reverse ordering is a different event.
    if refund.occurred_at < purchase.occurred_at:
        return None
    if refund.occurred_at - purchase.occurred_at > REFUND_WINDOW:
        return None

    score = 25
    features = ["refund_after_purchase"]
    if _same_amount(refund, purchase):
        score += 35
        features.append("exact_amount")
    elif (
        refund.amount_minor is not None
        and purchase.amount_minor is not None
        and 0 < refund.amount_minor < purchase.amount_minor
    ):
        score += 15
        features.append("partial_refund")
    if _similar_merchant(refund.merchant, purchase.merchant):
        score += 25
        features.append("similar_merchant")
    left_instrument = refund.instrument_id or refund.account_id
    right_instrument = purchase.instrument_id or purchase.account_id
    if left_instrument is not None and left_instrument == right_instrument:
        score += 20
        features.append("same_mapped_instrument")

    if score < MINIMUM_MATCH_SCORE:
        return None
    return MatchProposal(
        refund.observation_id,
        purchase.observation_id,
        "refund_match",
        min(100, score),
        tuple(features),
    )
