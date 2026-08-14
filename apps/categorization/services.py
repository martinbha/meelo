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
def apply_category_rule(*, transaction_id: Any, user: Any) -> CategoryRule | MerchantAlias | None:
    transaction = (
        CanonicalTransaction.objects.select_for_update()
        .filter(user=user, pk=transaction_id)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    if transaction.status == CanonicalTransaction.Status.CONFIRMED:
        raise ConflictError("Confirmed transactions require explicit category correction.")

    merchant_index = transaction.merchant_blind_index
    if not merchant_index:
        raise InvalidRequestError("Transaction merchant index is missing.")
    rule = (
        CategoryRule.objects.filter(
            user=user,
            is_active=True,
            rule_type=CategoryRule.RuleType.MERCHANT_EXACT,
            merchant_pattern_blind_index=merchant_index,
        )
        .filter(
            Q(payment_instrument__isnull=True)
            | Q(payment_instrument=transaction.payment_instrument)
        )
        .filter(
            Q(financial_account__isnull=True) | Q(financial_account=transaction.financial_account)
        )
        .annotate(
            scope_rank=Case(
                When(
                    payment_instrument__isnull=False,
                    payment_instrument=transaction.payment_instrument,
                    then=Value(0),
                ),
                When(financial_account=transaction.financial_account, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("-priority", "scope_rank", "created_at")
        .first()
    )
    match: CategoryRule | MerchantAlias | None = rule
    category = rule.category if rule else None
    if category is None:
        # Avoid hashing plaintext again: transaction ingestion already stored the scoped index.
        alias = (
            MerchantAlias.objects.filter(user=user, alias_blind_index=merchant_index)
            .filter(
                Q(payment_instrument=transaction.payment_instrument)
                | Q(payment_instrument__isnull=True)
            )
            .annotate(
                scope_rank=Case(
                    When(payment_instrument=transaction.payment_instrument, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("scope_rank", "created_at")
            .first()
        )
        match = alias
        category = alias.default_category if alias else None
    if category is None:
        return None
    assert match is not None
    transaction.category = category
    transaction.save(update_fields=("category", "updated_at"))
    record_audit_event(
        user=user,
        event_type="category_rule_applied",
        obj=transaction,
        metadata={"match_type": match._meta.model_name, "match_id": str(match.pk)},
    )
    return match
