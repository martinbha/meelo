from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.core.models import AuditEvent
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.forms import PaymentInstrumentMappingForm
from apps.instruments.models import PaymentInstrument


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


def make_account(user: Any, account_type: str, suffix: str) -> FinancialAccount:
    return FinancialAccount.objects.create(
        user=user,
        name_encrypted=suffix,
        name_blind_index=f"{suffix}-name",
        institution_encrypted="institution",
        institution_blind_index=f"{suffix}-institution",
        account_type=account_type,
    )


def make_instrument(
    user: Any,
    instrument_type: str,
    financial_account: FinancialAccount,
    settlement_account: FinancialAccount | None = None,
) -> PaymentInstrument:
    return PaymentInstrument(
        user=user,
        name_encrypted="instrument",
        name_blind_index=f"instrument-{instrument_type}",
        instrument_type=instrument_type,
        financial_account=financial_account,
        settlement_account=settlement_account,
    )


@pytest.mark.django_db
def test_debit_card_must_point_to_a_bank_or_cash_account(user: Any) -> None:
    liability = make_account(
        user,
        FinancialAccount.AccountType.CREDIT_CARD_LIABILITY,
        "liability",
    )
    instrument = make_instrument(user, PaymentInstrument.InstrumentType.DEBIT_CARD, liability)

    with pytest.raises(ValidationError, match="Debit cards"):
        instrument.full_clean()


@pytest.mark.django_db
def test_credit_card_requires_liability_and_valid_settlement_account(user: Any) -> None:
    bank = make_account(user, FinancialAccount.AccountType.CHECKING, "bank")
    instrument = make_instrument(user, PaymentInstrument.InstrumentType.CREDIT_CARD, bank)

    with pytest.raises(ValidationError, match="Credit cards"):
        instrument.full_clean()

    liability = make_account(
        user,
        FinancialAccount.AccountType.CREDIT_CARD_LIABILITY,
        "liability",
    )
    invalid_settlement = make_account(
        user,
        FinancialAccount.AccountType.LOAN,
        "loan",
    )
    instrument.financial_account = liability
    instrument.settlement_account = invalid_settlement

    with pytest.raises(ValidationError, match="settlements"):
        instrument.full_clean()


@pytest.mark.django_db
def test_mapping_accounts_must_belong_to_instrument_owner(user: Any) -> None:
    other = type(user).objects.create_user("other@example.com", password="password")
    account = make_account(other, FinancialAccount.AccountType.CHECKING, "other-bank")
    instrument = make_instrument(user, PaymentInstrument.InstrumentType.DEBIT_CARD, account)

    with pytest.raises(ValidationError, match="same user"):
        instrument.full_clean()


@pytest.mark.django_db
def test_valid_credit_card_mapping_passes(user: Any) -> None:
    liability = make_account(
        user,
        FinancialAccount.AccountType.CREDIT_CARD_LIABILITY,
        "liability",
    )
    bank = make_account(user, FinancialAccount.AccountType.SAVINGS, "bank")
    instrument = make_instrument(
        user,
        PaymentInstrument.InstrumentType.CREDIT_CARD,
        liability,
        bank,
    )

    instrument.full_clean()


@pytest.mark.django_db
def test_mapping_form_scopes_accounts_and_audits_changes(user: Any) -> None:
    liability = make_account(
        user, FinancialAccount.AccountType.CREDIT_CARD_LIABILITY, "form-liability"
    )
    bank = make_account(user, FinancialAccount.AccountType.CHECKING, "form-bank")
    other = type(user).objects.create_user("mapping-other@example.com", password="password")
    other_bank = make_account(other, FinancialAccount.AccountType.CHECKING, "other-form-bank")
    instrument = make_instrument(user, PaymentInstrument.InstrumentType.CREDIT_CARD, liability)
    instrument.save()
    form = PaymentInstrumentMappingForm(
        user=user,
        instance=instrument,
        data={
            "financial_account": liability.pk,
            "settlement_account": bank.pk,
            "is_active": "on",
        },
    )

    assert form.is_valid(), form.errors
    assert other_bank not in form.fields["financial_account"].queryset  # type: ignore[attr-defined]
    saved = form.save()
    assert saved.settlement_account_id == bank.pk
    assert user.audit_events.filter(
        event_type=AuditEvent.EventType.PAYMENT_INSTRUMENT_CHANGED
    ).exists()
