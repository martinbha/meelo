from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from django.db import transaction as db_transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.core.audit import record_audit_event
from apps.core.crypto import decrypt_model_field, encrypt_model_field
from apps.core.errors import ConflictError, InvalidRequestError
from apps.transactions.models import CanonicalTransaction

from .engine import CategoryDecision, CategorySource, classify
from .models import Category, CategoryRule, MerchantAlias


def normalize_merchant(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if not normalized:
        raise InvalidRequestError("Merchant text cannot be empty.")
    return normalized


def merchant_blind_index(value: str, *, user_id: Any, key: bytes) -> str:
    if len(key) < 32:
        raise InvalidRequestError("Blind-index keys must contain at least 32 bytes.")
    payload = f"merchant|{user_id}|{normalize_merchant(value)}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _check_owned(user: Any, **objects: Any) -> None:
    for name, obj in objects.items():
        if obj is not None and obj.user_id != user.pk:
            raise InvalidRequestError(f"The {name.replace('_', ' ')} does not belong to this user.")


@db_transaction.atomic
def create_merchant_alias(
    *,
    user: Any,
    alias: str,
    normalized_merchant: str,
    encryption_key: bytes,
    blind_index_key: bytes,
    key_version: int,
    default_category: Category | None = None,
    payment_instrument: Any | None = None,
) -> MerchantAlias:
    _check_owned(
        user,
        default_category=default_category,
        payment_instrument=payment_instrument,
    )
    alias_record = MerchantAlias(
        id=uuid.uuid4(),
        user=user,
        alias_blind_index=merchant_blind_index(alias, user_id=user.pk, key=blind_index_key),
        normalized_merchant_blind_index=merchant_blind_index(
            normalized_merchant, user_id=user.pk, key=blind_index_key
        ),
        default_category=default_category,
        payment_instrument=payment_instrument,
    )
    alias_record.alias_encrypted = encrypt_model_field(
        alias_record,
        "alias_encrypted",
        normalize_merchant(alias),
        key=encryption_key,
        key_version=key_version,
    )
    alias_record.normalized_merchant_encrypted = encrypt_model_field(
        alias_record,
        "normalized_merchant_encrypted",
        normalize_merchant(normalized_merchant),
        key=encryption_key,
        key_version=key_version,
    )
    alias_record.full_clean()
    alias_record.save()
    record_audit_event(
        user=user,
        event_type="merchant_alias_created",
        obj=alias_record,
        metadata={
            "has_default_category": default_category is not None,
            "card_scoped": payment_instrument is not None,
        },
    )
    return alias_record


def find_merchant_alias(
    *, user: Any, merchant: str, blind_index_key: bytes, payment_instrument_id: Any | None = None
) -> MerchantAlias | None:
    lookup = merchant_blind_index(merchant, user_id=user.pk, key=blind_index_key)
    return (
        MerchantAlias.objects.filter(user=user, alias_blind_index=lookup)
        .filter(Q(payment_instrument_id=payment_instrument_id) | Q(payment_instrument__isnull=True))
        .annotate(
            scope_rank=Case(
                When(payment_instrument_id=payment_instrument_id, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("scope_rank", "created_at")
        .first()
    )


def decrypt_normalized_merchant(alias: MerchantAlias, *, encryption_key: bytes) -> str:
    return decrypt_model_field(alias, "normalized_merchant_encrypted", key=encryption_key)


@db_transaction.atomic
def create_exact_merchant_rule(
    *,
    user: Any,
    merchant: str,
    category: Category,
    encryption_key: bytes,
    blind_index_key: bytes,
    key_version: int,
    payment_instrument: Any | None = None,
    financial_account: Any | None = None,
    priority: int = 0,
) -> CategoryRule:
    _check_owned(
        user,
        category=category,
        payment_instrument=payment_instrument,
        financial_account=financial_account,
    )
    rule = CategoryRule(
        id=uuid.uuid4(),
        user=user,
        category=category,
        merchant_pattern_blind_index=merchant_blind_index(
            merchant, user_id=user.pk, key=blind_index_key
        ),
        payment_instrument=payment_instrument,
        financial_account=financial_account,
        priority=priority,
    )
    rule.merchant_pattern_encrypted = encrypt_model_field(
        rule,
        "merchant_pattern_encrypted",
        normalize_merchant(merchant),
        key=encryption_key,
        key_version=key_version,
    )
    rule.full_clean()
    rule.save()
    record_audit_event(
        user=user,
        event_type="category_rule_created",
        obj=rule,
        metadata={"rule_type": rule.rule_type, "priority": priority},
    )
    return rule


@db_transaction.atomic
def set_rule_active(*, user: Any, rule_id: Any, is_active: bool) -> CategoryRule:
    rule = CategoryRule.objects.select_for_update().filter(user=user, pk=rule_id).first()
    if rule is None:
        raise InvalidRequestError("Category rule not found.")
    if rule.is_active == is_active:
        return rule
    rule.is_active = is_active
    rule.disabled_at = None if is_active else timezone.now()
    rule.save(update_fields=("is_active", "disabled_at", "updated_at"))
    record_audit_event(
        user=user,
        event_type="category_rule_enabled" if is_active else "category_rule_disabled",
        obj=rule,
    )
    return rule


@db_transaction.atomic
def categorize_transaction(*, transaction_id: Any, user: Any) -> CategoryDecision:
    """Apply the priority engine to one transaction and record what decided it.

    Repeating the call is safe and cheap: a decision that matches what is
    already stored writes nothing, so a bulk re-run does not churn every row's
    ``updated_at``.
    """

    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(user=user, pk=transaction_id)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    if transaction.status == CanonicalTransaction.Status.CONFIRMED:
        raise ConflictError("Confirmed transactions require explicit category correction.")

    decision = classify(transaction, user=user)
    if (
        transaction.category_id == (decision.category.pk if decision.category else None)
        and transaction.category_source == decision.source
    ):
        return decision

    transaction.category = decision.category
    transaction.category_source = decision.source
    transaction.save(update_fields=("category", "category_source", "updated_at"))
    if decision.is_categorized:
        record_audit_event(
            user=user,
            event_type="category_rule_applied",
            obj=transaction,
            metadata={
                "source": str(decision.source),
                "source_id": str(decision.source_id) if decision.source_id else "",
            },
        )
    return decision


@db_transaction.atomic
def set_category_manually(
    *, transaction_id: Any, user: Any, category: Category | None
) -> CategoryDecision:
    """Record the category the user chose, and stop guessing at this row.

    This is the explicit correction path a confirmed transaction is allowed to
    take: the category is the one part of a confirmed row a person is expected
    to keep refining, and refusing it would leave them with a total they know is
    wrong and cannot fix.
    """

    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(user=user, pk=transaction_id)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    _check_owned(user, category=category)

    transaction.category = category
    # Recorded as the user's own decision even when they cleared it: "none of
    # these" is an answer, and re-filing the row on the next run would undo it.
    transaction.category_source = CategorySource.MANUAL_OVERRIDE
    transaction.save(update_fields=("category", "category_source", "updated_at"))
    record_audit_event(
        user=user,
        event_type="category_changed",
        obj=transaction,
        metadata={
            "source": transaction.category_source,
            # The identifier, never the name: a category name is the user's own
            # words about their spending.
            "category_id": str(category.pk) if category is not None else "",
        },
    )
    return CategoryDecision(category, CategorySource(transaction.category_source), transaction.pk)
