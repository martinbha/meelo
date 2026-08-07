from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.ledger.models import ChartOfAccounts, LedgerAccount


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


@pytest.fixture
def chart(user: Any) -> ChartOfAccounts:
    return ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="personal",
        name_blind_index="personal-chart",
    )


def make_account(
    user: Any,
    chart: ChartOfAccounts,
    code: str,
    name: str,
    account_type: str,
    normal_balance: str,
    parent: LedgerAccount | None = None,
) -> LedgerAccount:
    return LedgerAccount.objects.create(
        user=user,
        chart=chart,
        code=code,
        name_encrypted=name,
        name_blind_index=f"{name}-index",
        account_type=account_type,
        normal_balance=normal_balance,
        parent=parent,
    )


@pytest.mark.django_db
def test_chart_and_ledger_accounts_support_hierarchy(user: Any, chart: ChartOfAccounts) -> None:
    assets = make_account(
        user,
        chart,
        "1000",
        "assets",
        LedgerAccount.AccountType.ASSET,
        LedgerAccount.NormalBalance.DEBIT,
    )
    cash = make_account(
        user,
        chart,
        "1100",
        "cash",
        LedgerAccount.AccountType.ASSET,
        LedgerAccount.NormalBalance.DEBIT,
        assets,
    )

    assert cash.ancestors() == [assets]
    assert list(assets.children.all()) == [cash]


@pytest.mark.django_db
def test_ledger_account_requires_matching_normal_balance(
    user: Any,
    chart: ChartOfAccounts,
) -> None:
    account = LedgerAccount(
        user=user,
        chart=chart,
        code="4000",
        name_encrypted="income",
        name_blind_index="income-index",
        account_type=LedgerAccount.AccountType.INCOME,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
    )

    with pytest.raises(ValidationError, match="Normal balance"):
        account.full_clean()


@pytest.mark.django_db
def test_ledger_account_rejects_parent_from_another_chart(
    user: Any, chart: ChartOfAccounts
) -> None:
    other_chart = ChartOfAccounts.objects.create(
        user=user,
        name_encrypted="other",
        name_blind_index="other-chart",
    )
    parent = make_account(
        user,
        other_chart,
        "1000",
        "other-assets",
        LedgerAccount.AccountType.ASSET,
        LedgerAccount.NormalBalance.DEBIT,
    )
    account = LedgerAccount(
        user=user,
        chart=chart,
        code="1100",
        name_encrypted="cash",
        name_blind_index="cash-index",
        account_type=LedgerAccount.AccountType.ASSET,
        normal_balance=LedgerAccount.NormalBalance.DEBIT,
        parent=parent,
    )

    with pytest.raises(ValidationError, match="same chart"):
        account.full_clean()
