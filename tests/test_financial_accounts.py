from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.financial_accounts.forms import FinancialAccountSettingsForm
from apps.financial_accounts.models import FinancialAccount


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.mark.django_db
def test_financial_account_stores_owner_and_account_metadata(user: Any) -> None:
    account = FinancialAccount.objects.create(
        id=uuid4(),
        user=user,
        name_encrypted="encrypted checking",
        name_blind_index="checking-index",
        institution_encrypted="encrypted institution",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.CHECKING,
        identifier_last_four="1234",
    )

    assert account.user_id == user.pk
    assert account.currency == "KRW"
    assert account.is_active is True


@pytest.mark.django_db
def test_financial_account_normalizes_currency_and_validates_suffix() -> None:
    account = FinancialAccount(
        id=uuid4(),
        name_encrypted="name",
        name_blind_index="name-index",
        institution_encrypted="institution",
        institution_blind_index="institution-index",
        account_type=FinancialAccount.AccountType.SAVINGS,
        currency="usd",
        identifier_last_four="123",
    )

    with pytest.raises(ValidationError) as error:
        account.full_clean()

    assert account.currency == "USD"
    assert "identifier_last_four" in error.value.message_dict


@pytest.mark.django_db
def test_financial_account_name_blind_index_is_unique_per_user(user: Any) -> None:
    values = {
        "user": user,
        "name_encrypted": "name",
        "name_blind_index": "same-index",
        "institution_encrypted": "institution",
        "institution_blind_index": "institution-index",
        "account_type": FinancialAccount.AccountType.CHECKING,
    }
    FinancialAccount.objects.create(id=uuid4(), **values)

    with pytest.raises(IntegrityError):
        FinancialAccount.objects.create(id=uuid4(), **values)


@pytest.mark.django_db
def test_database_rejects_invalid_account_types(user: Any) -> None:
    with pytest.raises(IntegrityError):
        FinancialAccount.objects.create(
            user=user,
            name_encrypted="invalid",
            name_blind_index="invalid-type",
            institution_encrypted="bank",
            institution_blind_index="invalid-type-bank",
            account_type="not-an-account-type",
        )


@pytest.mark.django_db
def test_account_settings_form_rejects_cross_owner_instances(user: Any) -> None:
    other = type(user).objects.create_user("form-other@example.com", password="password")
    account = FinancialAccount.objects.create(
        user=other,
        name_encrypted="other",
        name_blind_index="other-settings",
        institution_encrypted="bank",
        institution_blind_index="other-settings-bank",
        account_type=FinancialAccount.AccountType.CHECKING,
    )
    form = FinancialAccountSettingsForm(
        user=user,
        instance=account,
        data={
            "account_type": FinancialAccount.AccountType.SAVINGS,
            "currency": "KRW",
            "identifier_last_four": "1234",
            "is_active": "on",
        },
    )

    assert form.is_valid() is False
    assert "not found" in form.non_field_errors()[0]
