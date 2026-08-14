from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.core.audit import record_audit_event
from apps.core.errors import InvalidRequestError
from apps.financial_accounts.models import FinancialAccount

from .models import PaymentInstrument


@transaction.atomic
def update_instrument_mapping(
    instrument_id: Any,
    *,
    user: Any,
    financial_account: FinancialAccount,
    settlement_account: FinancialAccount | None,
    is_active: bool,
) -> PaymentInstrument:
    instrument = (
        PaymentInstrument.objects.select_for_update().filter(pk=instrument_id, user=user).first()
    )
    if instrument is None:
        raise InvalidRequestError("Payment instrument not found.")
    instrument.financial_account = financial_account
    instrument.settlement_account = settlement_account
    instrument.is_active = is_active
    instrument.full_clean()
    instrument.save(
        update_fields=("financial_account", "settlement_account", "is_active", "updated_at")
    )
    record_audit_event(
        user=user,
        event_type="payment_instrument_changed",
        obj=instrument,
        metadata={
            "financial_account_id": str(financial_account.pk),
            "settlement_account_id": str(settlement_account.pk) if settlement_account else "",
            "is_active": is_active,
        },
    )
    return instrument
