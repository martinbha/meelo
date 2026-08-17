"""Deciding a transaction's category, and saying what decided it.

Categorization is a stack of increasingly general guesses, and the order they
are tried in is the whole design (specification 18). A rule the user wrote
themselves beats a name the system learned; anything the user has already
corrected beats every guess; and when nothing applies, the transaction is
*uncategorized* rather than quietly filed somewhere plausible. A wrong category
that looks confident is worse than an empty one, because only the empty one
gets fixed.

Two properties follow from that and are worth stating plainly:

- **Every decision names its source.** A category with no explanation cannot be
  argued with, and the user is the one who has to argue with it.
- **A manual correction is never overwritten.** Re-running classification over a
  row the user has already answered would undo their work on a schedule.

Matching happens on blind indexes, so a rule is found without decrypting every
merchant in the database. That also bounds what this module can express: an
index supports equality and nothing else, so substring rules are left to the
work that gives them somewhere to run (#191).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from django.db.models import Case, IntegerField, Q, Value, When

from apps.transactions.models import CanonicalTransaction

from .models import Category, CategoryRule, MerchantAlias


class CategorySource(StrEnum):
    """Where a category came from, named in the order the sources are tried."""

    #: The user said so on this transaction. Nothing overrides it.
    MANUAL_OVERRIDE = "manual_override"
    #: An exact merchant rule the user wrote.
    USER_RULE = "user_rule"
    #: A rule covering everything on one card.
    CARD_RULE = "card_rule"
    #: A learned name for a merchant, carrying a default category.
    MERCHANT_ALIAS = "merchant_alias"
    #: A rule keyed on who was paid rather than what was bought.
    COUNTERPARTY_RULE = "counterparty_rule"
    #: The category the user chose last time this merchant appeared.
    PRIOR_CONFIRMATION = "prior_confirmation"
    #: Whatever the parser guessed when the row was imported.
    PARSER = "parser"
    #: Nothing applied, and saying so is the honest answer.
    UNCATEGORIZED = "uncategorized"


#: The order sources are consulted in. First hit wins, and the order never
#: varies with the data — two runs over the same transaction agree.
PRECEDENCE: tuple[CategorySource, ...] = (
    CategorySource.MANUAL_OVERRIDE,
    CategorySource.USER_RULE,
    CategorySource.CARD_RULE,
    CategorySource.MERCHANT_ALIAS,
    CategorySource.COUNTERPARTY_RULE,
    CategorySource.PRIOR_CONFIRMATION,
    CategorySource.PARSER,
    CategorySource.UNCATEGORIZED,
)

#: Sources that represent a decision the user made rather than one the system
#: guessed. Re-classification leaves these alone.
USER_DECIDED: frozenset[str] = frozenset({CategorySource.MANUAL_OVERRIDE})


@dataclass(frozen=True, slots=True)
class CategoryDecision:
    """A category and the reason it was chosen."""

    category: Category | None
    source: CategorySource
    rule_id: Any = None

    @property
    def is_categorized(self) -> bool:
        return self.category is not None

    @property
    def is_user_decided(self) -> bool:
        return self.source in USER_DECIDED


def _scoped_to(transaction: CanonicalTransaction) -> Q:
    """Rules that either apply everywhere or apply to this transaction's card."""

    return (
        Q(payment_instrument__isnull=True)
        | Q(payment_instrument_id=transaction.payment_instrument_id)
    ) & (
        Q(financial_account__isnull=True) | Q(financial_account_id=transaction.financial_account_id)
    )


def _by_specificity(transaction: CanonicalTransaction, queryset: Any) -> Any:
    """Order rules so the narrowest scope wins a tie on priority.

    A rule written for one card is a more deliberate statement than one written
    for everything, so it goes first when both match.
    """

    return queryset.annotate(
        scope_rank=Case(
            When(payment_instrument_id=transaction.payment_instrument_id, then=Value(0)),
            When(financial_account_id=transaction.financial_account_id, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by("-priority", "scope_rank", "created_at")


def _user_rule(transaction: CanonicalTransaction, user: Any) -> CategoryDecision | None:
    if not transaction.merchant_blind_index:
        return None
    rule = _by_specificity(
        transaction,
        CategoryRule.objects.filter(
            user_id=user.pk,
            is_active=True,
            rule_type=CategoryRule.RuleType.MERCHANT_EXACT,
            merchant_pattern_blind_index=transaction.merchant_blind_index,
        ).filter(_scoped_to(transaction)),
    ).first()
    if rule is None:
        return None
    return CategoryDecision(rule.category, CategorySource.USER_RULE, rule.pk)


def _card_rule(transaction: CanonicalTransaction, user: Any) -> CategoryDecision | None:
    if transaction.payment_instrument_id is None:
        return None
    rule = (
        CategoryRule.objects.filter(
            user_id=user.pk,
            is_active=True,
            rule_type=CategoryRule.RuleType.PAYMENT_INSTRUMENT,
            payment_instrument_id=transaction.payment_instrument_id,
        )
        .order_by("-priority", "created_at")
        .first()
    )
    if rule is None:
        return None
    return CategoryDecision(rule.category, CategorySource.CARD_RULE, rule.pk)


def _merchant_alias(transaction: CanonicalTransaction, user: Any) -> CategoryDecision | None:
    if not transaction.merchant_blind_index:
        return None
    alias = (
        MerchantAlias.objects.filter(
            user_id=user.pk,
            alias_blind_index=transaction.merchant_blind_index,
            default_category__isnull=False,
        )
        .filter(
            Q(payment_instrument_id=transaction.payment_instrument_id)
            | Q(payment_instrument__isnull=True)
        )
        .annotate(
            scope_rank=Case(
                When(payment_instrument_id=transaction.payment_instrument_id, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("scope_rank", "created_at")
        .first()
    )
    if alias is None:
        return None
    return CategoryDecision(alias.default_category, CategorySource.MERCHANT_ALIAS, alias.pk)


def _counterparty_rule(transaction: CanonicalTransaction, user: Any) -> CategoryDecision | None:
    if not transaction.counterparty_blind_index:
        return None
    rule = _by_specificity(
        transaction,
        CategoryRule.objects.filter(
            user_id=user.pk,
            is_active=True,
            rule_type=CategoryRule.RuleType.COUNTERPARTY_EXACT,
            merchant_pattern_blind_index=transaction.counterparty_blind_index,
        ).filter(_scoped_to(transaction)),
    ).first()
    if rule is None:
        return None
    return CategoryDecision(rule.category, CategorySource.COUNTERPARTY_RULE, rule.pk)


def _prior_confirmation(transaction: CanonicalTransaction, user: Any) -> CategoryDecision | None:
    """The category the user chose the last time this merchant appeared.

    Weaker than any written rule — the user filed one transaction, they did not
    state a policy — but stronger than a parser guess, because it is still a
    decision they made rather than one the system made for them.
    """

    if not transaction.merchant_blind_index:
        return None
    previous = (
        CanonicalTransaction.objects.filter(
            user_id=user.pk,
            merchant_blind_index=transaction.merchant_blind_index,
            category_source=CategorySource.MANUAL_OVERRIDE,
            category__isnull=False,
        )
        .exclude(pk=transaction.pk)
        .order_by("-occurred_at", "-created_at")
        .first()
    )
    if previous is None:
        return None
    return CategoryDecision(previous.category, CategorySource.PRIOR_CONFIRMATION, previous.pk)


def classify(transaction: CanonicalTransaction, *, user: Any) -> CategoryDecision:
    """Decide this transaction's category and say what decided it.

    Pure with respect to the database: nothing is written here, so a caller can
    preview the answer before applying it.
    """

    if transaction.category_source in USER_DECIDED and transaction.category_id is not None:
        # The user has already answered. Re-running classification over their
        # answer would undo their work on a schedule.
        return CategoryDecision(
            transaction.category, CategorySource.MANUAL_OVERRIDE, transaction.pk
        )

    for resolver in (
        _user_rule,
        _card_rule,
        _merchant_alias,
        _counterparty_rule,
        _prior_confirmation,
    ):
        decision = resolver(transaction, user)
        if decision is not None and decision.category is not None:
            return decision

    if transaction.category_id is not None:
        # Whatever the parser guessed at import survives when no rule speaks.
        return CategoryDecision(transaction.category, CategorySource.PARSER)
    return CategoryDecision(None, CategorySource.UNCATEGORIZED)
