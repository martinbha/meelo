"""Turning one correction into a rule, at a scope the user chooses.

A reviewer who re-files a coffee shop from "groceries" to "eating out" has just
told the system something it can reuse. But how far it reuses it is a question
only they can answer: this one row, every transaction from that merchant, or
every transaction from that merchant on that card. Guessing produces a rule
nobody asked for, quietly reclassifying a year of history from one correction
(specification 18).

So three things hold here:

- **The scope is chosen before the rule is written**, never inferred.
- **The user sees what the rule would touch before it exists.**
  :func:`preview_rule` counts what a new rule would reach, including the
  confirmed rows it would deliberately leave alone.
- **Confirmed history is never rewritten.** A new rule applies to what comes
  next. Applying it backwards is possible, but it takes a second, explicit
  request, and even then it stops at rows the user has already confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from django.db import transaction as db_transaction

from apps.core.audit import record_audit_event
from apps.core.blind_index import SearchKey
from apps.core.errors import InvalidRequestError
from apps.observations.models import ImportedObservation
from apps.transactions.models import CanonicalTransaction

from .engine import CategorySource, classify
from .models import Category, CategoryRule
from .services import create_exact_merchant_rule, set_category_manually, store_decision


class RuleScope(StrEnum):
    """How widely a correction should apply."""

    #: This row and nothing else. No rule is written.
    TRANSACTION_ONLY = "transaction_only"
    #: Everything from this merchant, on any card.
    MERCHANT = "merchant"
    #: Everything from this merchant on the card this transaction used.
    MERCHANT_AND_CARD = "merchant_and_card"


SCOPE_LABELS: dict[RuleScope, str] = {
    RuleScope.TRANSACTION_ONLY: "Only this transaction",
    RuleScope.MERCHANT: "Every transaction from this merchant",
    RuleScope.MERCHANT_AND_CARD: "This merchant, on this card only",
}


@dataclass(frozen=True, slots=True)
class RulePreview:
    """What a rule at this scope would reach, before it exists."""

    scope: RuleScope
    #: Rows still waiting for review that the rule would categorise.
    pending_observations: int
    #: Draft transactions the rule would change if applied backwards.
    draft_transactions: int
    #: Confirmed transactions the rule matches and will not touch. Counted and
    #: shown rather than hidden: a user deciding on a scope should see the
    #: history that stays as it is.
    confirmed_transactions: int
    #: Transactions the user categorised by hand, which outrank any rule.
    manually_categorized: int

    @property
    def writes_a_rule(self) -> bool:
        return self.scope is not RuleScope.TRANSACTION_ONLY

    @property
    def touches_nothing_yet(self) -> bool:
        return self.pending_observations == 0 and self.draft_transactions == 0


def _resolve_scope(value: str | RuleScope) -> RuleScope:
    try:
        return RuleScope(value)
    except ValueError as exc:
        raise InvalidRequestError(f"Unknown rule scope: {value!r}.") from exc


def _matching_transactions(
    transaction: CanonicalTransaction, *, user: Any, scope: RuleScope
) -> Any:
    queryset = CanonicalTransaction.objects.filter(
        user_id=user.pk, merchant_blind_index=transaction.merchant_blind_index
    )
    if scope is RuleScope.MERCHANT_AND_CARD:
        queryset = queryset.filter(payment_instrument_id=transaction.payment_instrument_id)
    return queryset


def preview_rule(
    *, user: Any, transaction: CanonicalTransaction, scope: str | RuleScope
) -> RulePreview:
    """Count what a rule at this scope would reach. Writes nothing."""

    resolved = _resolve_scope(scope)
    if resolved is RuleScope.TRANSACTION_ONLY:
        return RulePreview(resolved, 0, 0, 0, 0)
    if not transaction.merchant_blind_index:
        raise InvalidRequestError("A merchant rule needs a merchant to key on.")
    if resolved is RuleScope.MERCHANT_AND_CARD and transaction.payment_instrument_id is None:
        raise InvalidRequestError("A card-scoped rule needs a card.")

    observations = ImportedObservation.objects.filter(
        user_id=user.pk,
        merchant_blind_index=transaction.merchant_blind_index,
        review_status=ImportedObservation.ReviewStatus.UNREVIEWED,
    )
    if resolved is RuleScope.MERCHANT_AND_CARD:
        observations = observations.filter(
            payment_instrument_guess_id=transaction.payment_instrument_id
        )

    matching = _matching_transactions(transaction, user=user, scope=resolved)
    return RulePreview(
        scope=resolved,
        pending_observations=observations.count(),
        # The transaction being corrected is excluded: it is handled by the
        # correction itself, and counting it would promise one more move than
        # applying the rule backwards actually makes.
        draft_transactions=matching.filter(status=CanonicalTransaction.Status.DRAFT)
        .exclude(pk=transaction.pk)
        .exclude(category_source=CategorySource.MANUAL_OVERRIDE)
        .count(),
        confirmed_transactions=matching.filter(
            status=CanonicalTransaction.Status.CONFIRMED
        ).count(),
        manually_categorized=matching.filter(
            category_source=CategorySource.MANUAL_OVERRIDE
        ).count(),
    )


@dataclass(frozen=True, slots=True)
class RuleCreationResult:
    """What was written, and what it changed."""

    scope: RuleScope
    rule: CategoryRule | None
    #: Draft transactions reclassified because the user asked for it.
    reclassified: int = 0


@db_transaction.atomic
def create_rule_from_correction(
    *,
    user: Any,
    transaction: CanonicalTransaction,
    category: Category,
    scope: str | RuleScope,
    blind_index_key: SearchKey | bytes,
    encryption_key: bytes,
    key_version: int = 1,
    merchant: str,
    apply_to_existing: bool = False,
) -> RuleCreationResult:
    """Record the correction, and a rule at the scope the user picked.

    The correction itself always lands on this transaction. Whether it becomes
    a rule, and how far that rule reaches backwards, are the two decisions the
    caller has to have made already.
    """

    resolved = _resolve_scope(scope)
    if transaction.user_id != user.pk:
        raise InvalidRequestError("The transaction does not belong to this user.")

    set_category_manually(transaction_id=transaction.pk, user=user, category=category)
    if resolved is RuleScope.TRANSACTION_ONLY:
        return RuleCreationResult(resolved, None)

    if resolved is RuleScope.MERCHANT_AND_CARD and transaction.payment_instrument_id is None:
        raise InvalidRequestError("A card-scoped rule needs a card.")
    rule = create_exact_merchant_rule(
        user=user,
        merchant=merchant,
        category=category,
        encryption_key=encryption_key,
        blind_index_key=blind_index_key,
        key_version=key_version,
        payment_instrument=(
            transaction.payment_instrument if resolved is RuleScope.MERCHANT_AND_CARD else None
        ),
    )
    record_audit_event(
        user=user,
        event_type="category_rule_created",
        obj=rule,
        metadata={
            "scope": str(resolved),
            "source": "review_correction",
            "applied_to_existing": apply_to_existing,
        },
    )

    reclassified = 0
    if apply_to_existing:
        reclassified = _apply_backwards(transaction, user=user, scope=resolved)
    return RuleCreationResult(resolved, rule, reclassified)


def _apply_backwards(transaction: CanonicalTransaction, *, user: Any, scope: RuleScope) -> int:
    """Reclassify draft transactions the new rule now covers.

    Confirmed rows are excluded, and so are rows the user categorised by hand:
    a rule the user wrote a minute ago does not get to overrule a decision they
    made deliberately about one transaction.
    """

    candidates = (
        _matching_transactions(transaction, user=user, scope=scope)
        .filter(status=CanonicalTransaction.Status.DRAFT)
        .exclude(category_source=CategorySource.MANUAL_OVERRIDE)
        .select_for_update()
    )
    return sum(
        1 for candidate in candidates if store_decision(candidate, classify(candidate, user=user))
    )
