from typing import Any

import pytest

from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def account(user: Any) -> FinancialAccount:
    return FinancialAccount.objects.create(
        user=user,
        name_encrypted="checking",
        name_blind_index="checking-index",
        institution_encrypted="institution",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
    )


@pytest.mark.django_db
def test_payment_instrument_links_owner_and_accounts(user: Any, account: FinancialAccount) -> None:
    instrument = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="card",
        name_blind_index="card-index",
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        last_four="1234",
        financial_account=account,
        issuer_encrypted="issuer",
    )

    assert instrument.user_id == user.pk
    assert instrument.financial_account_id == account.pk
    assert instrument.settlement_account_id is None


@pytest.mark.django_db
def test_credit_card_can_reference_a_separate_settlement_account(
    user: Any,
    account: FinancialAccount,
) -> None:
    liability = FinancialAccount.objects.create(
        user=user,
        name_encrypted="liability",
        name_blind_index="liability-index",
        institution_encrypted="institution",
        institution_blind_index="liability-institution-index",
        account_type=FinancialAccount.AccountType.CREDIT_CARD_LIABILITY,
    )

    instrument = PaymentInstrument.objects.create(
        user=user,
        name_encrypted="credit card",
        name_blind_index="credit-card-index",
        instrument_type=PaymentInstrument.InstrumentType.CREDIT_CARD,
        last_four="9876",
        financial_account=liability,
        settlement_account=account,
    )

    assert instrument.financial_account.account_type == "credit_card_liability"
    assert instrument.settlement_account_id == account.pk
