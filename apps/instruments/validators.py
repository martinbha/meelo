from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError

from apps.financial_accounts.models import FinancialAccount

if TYPE_CHECKING:
    from .models import PaymentInstrument


def validate_payment_instrument_mapping(instrument: PaymentInstrument) -> None:
    """Validate card-to-account relationships before a mapping is accepted."""

    errors: dict[str, str] = {}
    bank_account_types = {
        FinancialAccount.AccountType.CHECKING,
        FinancialAccount.AccountType.SAVINGS,
        FinancialAccount.AccountType.CASH,
    }

    if instrument.last_four and len(instrument.last_four) != 4:
        errors["last_four"] = "The instrument suffix must be four characters."

    if instrument.financial_account_id and instrument.user_id:
        account_owner = (
            FinancialAccount.objects.filter(pk=instrument.financial_account_id)
            .values_list("user_id", flat=True)
            .first()
        )
        if account_owner != instrument.user_id:
            errors["financial_account"] = "The financial account must belong to the same user."

    if instrument.settlement_account_id and instrument.user_id:
        settlement_owner = (
            FinancialAccount.objects.filter(pk=instrument.settlement_account_id)
            .values_list("user_id", flat=True)
            .first()
        )
        if settlement_owner != instrument.user_id:
            errors["settlement_account"] = "The settlement account must belong to the same user."

    if instrument.financial_account_id == instrument.settlement_account_id:
        errors["settlement_account"] = "A settlement account must differ from the card account."

    account_type = None
    if instrument.financial_account_id:
        account_type = (
            FinancialAccount.objects.filter(pk=instrument.financial_account_id)
            .values_list("account_type", flat=True)
            .first()
        )

    if instrument.instrument_type == instrument.InstrumentType.DEBIT_CARD:
        if account_type not in bank_account_types:
            errors["financial_account"] = "Debit cards must point to a bank or cash account."
        if instrument.settlement_account_id:
            errors["settlement_account"] = "Debit cards cannot have a separate settlement account."
    elif instrument.instrument_type == instrument.InstrumentType.CREDIT_CARD:
        if account_type != FinancialAccount.AccountType.CREDIT_CARD_LIABILITY:
            errors["financial_account"] = "Credit cards must point to a liability account."
        if instrument.settlement_account_id:
            settlement_type = (
                FinancialAccount.objects.filter(pk=instrument.settlement_account_id)
                .values_list("account_type", flat=True)
                .first()
            )
            if settlement_type not in bank_account_types:
                errors["settlement_account"] = (
                    "Credit-card settlements must point to a bank or cash account."
                )

    if errors:
        raise ValidationError(errors)
