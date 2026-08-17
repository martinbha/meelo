from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction as db_transaction

from apps.categorization.models import Category
from apps.core.audit import record_audit_event
from apps.core.crypto import encrypt_model_fields
from apps.core.errors import ConflictError, InvalidRequestError
from apps.core.ownership import owned_queryset
from apps.core.value_objects import Currency, Money
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.ledger.models import LedgerEntry

from .invariants import validate_transaction_invariants
from .models import CanonicalTransaction
from .money import store_money


def _validate_related_objects(
    *,
    user: Any,
    account: FinancialAccount,
    instrument: PaymentInstrument | None,
    category: Category | None,
) -> None:
    if account.user_id != user.pk:
        raise InvalidRequestError("The financial account does not belong to this user.")
    if instrument is not None and (
        instrument.user_id != user.pk or instrument.financial_account_id != account.pk
    ):
        raise InvalidRequestError("The payment instrument is not compatible with the account.")
    if category is not None and category.user_id != user.pk:
        raise InvalidRequestError("The category does not belong to this user.")


def _amount(amount_minor: int | Decimal, currency: str) -> Money:
    try:
        minor = int(amount_minor)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("Amount must be an integer number of minor units.") from exc
    if minor <= 0:
        raise InvalidRequestError("Amount must be greater than zero.")
    return Money(minor, Currency(currency))


#: The fields on a transaction that hold a value rather than a reference. All of
#: them are encrypted together once the row has an identity, because the
#: associated data binds each one to this record.
VALUE_FIELDS = ("merchant_encrypted", "counterparty_encrypted", "notes_encrypted")


@db_transaction.atomic
def create_manual_transaction(
    *,
    user: Any,
    occurred_at: date,
    amount_minor: int | Decimal,
    currency: str,
    transaction_type: str,
    financial_account: FinancialAccount,
    payment_instrument: PaymentInstrument | None = None,
    category: Category | None = None,
    merchant: str = "",
    counterparty: str = "",
    notes: str = "",
    data_key: bytes | None = None,
    key_version: int = 1,
) -> CanonicalTransaction:
    _validate_related_objects(
        user=user, account=financial_account, instrument=payment_instrument, category=category
    )
    transaction = CanonicalTransaction(
        user=user,
        created_by=user,
        occurred_at=occurred_at,
        currency=currency,
        transaction_type=transaction_type,
        financial_account=financial_account,
        payment_instrument=payment_instrument,
        category=category,
        status=CanonicalTransaction.Status.DRAFT,
    )
    store_money(
        transaction,
        "amount_encrypted",
        _amount(amount_minor, currency),
        data_key=data_key,
        key_version=key_version,
    )
    if data_key is not None:
        encrypt_model_fields(
            transaction,
            {
                "merchant_encrypted": merchant,
                "counterparty_encrypted": counterparty,
                "notes_encrypted": notes,
            },
            key=data_key,
            key_version=key_version,
        )
    else:
        transaction.merchant_encrypted = merchant
        transaction.counterparty_encrypted = counterparty
        transaction.notes_encrypted = notes
    validate_transaction_invariants(transaction, data_key=data_key)
    transaction.save()
    record_audit_event(
        user=user,
        event_type="transaction_created",
        obj=transaction,
        metadata={"source": "manual", "transaction_type": transaction.transaction_type},
    )
    return transaction


@db_transaction.atomic
def update_manual_transaction(
    transaction_id: Any,
    *,
    user: Any,
    occurred_at: date,
    amount_minor: int | Decimal,
    currency: str,
    transaction_type: str,
    financial_account: FinancialAccount,
    payment_instrument: PaymentInstrument | None = None,
    category: Category | None = None,
    merchant: str = "",
    counterparty: str = "",
    notes: str = "",
    data_key: bytes | None = None,
    key_version: int = 1,
) -> CanonicalTransaction:
    transaction = (
        owned_queryset(CanonicalTransaction, user)
        .select_for_update()
        .filter(pk=transaction_id)
        .first()
    )
    if transaction is None:
        raise InvalidRequestError("Transaction not found.")
    if (
        transaction.status == CanonicalTransaction.Status.CONFIRMED
        and LedgerEntry.objects.filter(transaction=transaction).exists()
    ):
        raise ConflictError("Posted transactions cannot be edited.")
    _validate_related_objects(
        user=user, account=financial_account, instrument=payment_instrument, category=category
    )
    transaction.occurred_at = occurred_at
    transaction.currency = currency
    transaction.transaction_type = transaction_type
    transaction.financial_account = financial_account
    transaction.payment_instrument = payment_instrument
    transaction.category = category
    store_money(
        transaction,
        "amount_encrypted",
        _amount(amount_minor, currency),
        data_key=data_key,
        key_version=key_version,
    )
    if data_key is not None:
        transaction.merchant_encrypted = ""
        transaction.counterparty_encrypted = ""
        transaction.notes_encrypted = ""
        encrypt_model_fields(
            transaction,
            {
                "merchant_encrypted": merchant,
                "counterparty_encrypted": counterparty,
                "notes_encrypted": notes,
            },
            key=data_key,
            key_version=key_version,
        )
    else:
        transaction.merchant_encrypted = merchant
        transaction.counterparty_encrypted = counterparty
        transaction.notes_encrypted = notes
    validate_transaction_invariants(transaction, data_key=data_key)
    transaction.save()
    record_audit_event(
        user=user,
        event_type="transaction_corrected",
        obj=transaction,
        metadata={"source": "manual", "transaction_type": transaction.transaction_type},
    )
    return transaction
