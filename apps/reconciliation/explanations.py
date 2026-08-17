"""Saying why two rows were paired, in words a person can check.

A score on its own is not a reason. "87" tells a reviewer how confident the
matcher was, not what it noticed, and a reviewer who cannot see the evidence can
only defer to the number — which is exactly the deference this system is built
to avoid. Every proposal therefore carries the feature names that produced its
score, and this module turns those names into sentences.

The stored feature names are deliberately names and never values: the audit log
and the match row must not become a second, unencrypted copy of the financial
data they describe.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apps.transactions.models import CanonicalTransaction

from .models import ReconciliationMatch

_Type = CanonicalTransaction.TransactionType
_Match = ReconciliationMatch.MatchType

#: One sentence per feature the matchers and the duplicate scorer can emit.
FEATURE_LABELS: Mapping[str, str] = {
    "exact_amount": "The amounts are identical.",
    "approximate_amount": "The amounts are close, but not identical.",
    "partial_refund": "The credit is smaller than the purchase it may reverse.",
    "partial_payment": "The payment is smaller than the outstanding balance.",
    "amount_matches_statement": "The payment equals the statement amount.",
    "amount_matches_outstanding_balance": "The payment equals the outstanding balance.",
    "same_approval_code": "Both rows carry the same approval code.",
    "same_date": "Both rows fall on the same date.",
    "nearby_date": "The two dates are within a day of each other.",
    "refund_after_purchase": "The credit follows the purchase it may reverse.",
    "settlement_window": "The payment falls within a billing cycle of the statement.",
    "compatible_direction": "One row is money out and the other matches it.",
    "opposite_directions": "One row is money out and the other money in.",
    "same_direction": "Both rows move money the same way.",
    "similar_merchant": "The merchant names are alike.",
    "same_merchant": "The merchant names are the same.",
    "card_mapped_to_account": "The card is mapped to the bank account on the other row.",
    "configured_settlement_account": "The withdrawal comes from the configured settlement account.",
    "issuer_counterparty": "The counterparty looks like a card issuer being paid.",
    "both_accounts_owned": "Both rows sit in accounts you own.",
    "same_mapped_instrument": "Both rows sit on the same card or account.",
    "same_account": "Both rows sit in the same account.",
    "same_balance_after": "Both rows leave the same balance behind.",
    "same_source_type": "Both rows came from the same kind of screenshot.",
    "reviewer_tolerance": "Found only because the search was widened on request.",
    "manual_link": "You linked these two rows yourself.",
}

#: The canonical transaction type a confirmed match would produce. A duplicate
#: has none: merging two views of one event does not decide what that event was.
PROPOSED_TRANSACTION_TYPE: Mapping[str, str] = {
    _Match.DEBIT_CARD_BANK_MATCH: _Type.PURCHASE,
    _Match.CREDIT_CARD_PAYMENT: _Type.CREDIT_CARD_PAYMENT,
    _Match.INTERNAL_TRANSFER: _Type.INTERNAL_TRANSFER,
    _Match.REFUND_MATCH: _Type.REFUND,
    _Match.STATEMENT_MEMBERSHIP: _Type.CREDIT_CARD_PAYMENT,
}


def describe_features(features: Sequence[str]) -> tuple[str, ...]:
    """Turn stored feature names into reasons, keeping unknown names visible.

    A name with no sentence yet is shown as itself rather than dropped: a
    proposal that quietly displayed fewer reasons than it was scored on would
    misrepresent its own evidence.
    """

    return tuple(FEATURE_LABELS.get(name, name) for name in features)


def proposed_transaction_type(match_type: str) -> str | None:
    """The transaction type confirming this match would produce, if any."""

    return PROPOSED_TRANSACTION_TYPE.get(match_type)


def proposed_transaction_type_label(match_type: str) -> str:
    """The human-readable form of :func:`proposed_transaction_type`."""

    value = proposed_transaction_type(match_type)
    if value is None:
        return ""
    return str(_Type(value).label)
