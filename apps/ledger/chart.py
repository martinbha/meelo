"""Finding — or creating — the ledger accounts a posting needs.

The chart of accounts is per user and is not something a person is asked to
design. Somebody opening a savings account wants a savings account, not a
conversation about whether it is account 1010 or 1020. So the accounts a posting
requires are created on demand, deterministically, and the same call twice
returns the same account rather than a second one.

Two rules shape the mapping.

**A financial account's ledger type follows from what it is.** Checking,
savings, cash, and investments are assets; a card liability and a loan are
liabilities. The user never states a normal balance, because getting it wrong is
how a card balance ends up growing as it is paid off.

**Names are re-encrypted rather than copied.** A ciphertext is bound to the
record, field, and owner it was written for, so moving a financial account's
``name_encrypted`` onto a ledger account would produce a value that cannot be
opened. The caller supplies the plaintext and the key, and the ledger account
gets its own ciphertext.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction as db_transaction

from apps.core.errors import InvalidRequestError
from apps.financial_accounts.models import FinancialAccount

from .models import ChartOfAccounts, LedgerAccount

#: The chart every user gets. One chart is enough for a household of one, and a
#: second one would only raise the question of which the reports read.
DEFAULT_CHART_BLIND_INDEX = "default-chart"
DEFAULT_CHART_NAME = "Personal"

#: Leading digit per ledger account type, following the usual convention so a
#: printed chart reads the way an accountant expects.
CODE_PREFIXES: dict[str, str] = {
    LedgerAccount.AccountType.ASSET: "1",
    LedgerAccount.AccountType.LIABILITY: "2",
    LedgerAccount.AccountType.EQUITY: "3",
    LedgerAccount.AccountType.INCOME: "4",
    LedgerAccount.AccountType.EXPENSE: "5",
}

NORMAL_BALANCES: dict[str, str] = {
    LedgerAccount.AccountType.ASSET: LedgerAccount.NormalBalance.DEBIT,
    LedgerAccount.AccountType.EXPENSE: LedgerAccount.NormalBalance.DEBIT,
    LedgerAccount.AccountType.LIABILITY: LedgerAccount.NormalBalance.CREDIT,
    LedgerAccount.AccountType.EQUITY: LedgerAccount.NormalBalance.CREDIT,
    LedgerAccount.AccountType.INCOME: LedgerAccount.NormalBalance.CREDIT,
}

#: What each kind of real-world account is, in ledger terms.
FINANCIAL_ACCOUNT_LEDGER_TYPES: dict[str, str] = {
    FinancialAccount.AccountType.CHECKING: LedgerAccount.AccountType.ASSET,
    FinancialAccount.AccountType.SAVINGS: LedgerAccount.AccountType.ASSET,
    FinancialAccount.AccountType.CASH: LedgerAccount.AccountType.ASSET,
    FinancialAccount.AccountType.INVESTMENT: LedgerAccount.AccountType.ASSET,
    FinancialAccount.AccountType.OTHER_ASSET: LedgerAccount.AccountType.ASSET,
    FinancialAccount.AccountType.CREDIT_CARD_LIABILITY: LedgerAccount.AccountType.LIABILITY,
    FinancialAccount.AccountType.LOAN: LedgerAccount.AccountType.LIABILITY,
    FinancialAccount.AccountType.OTHER_LIABILITY: LedgerAccount.AccountType.LIABILITY,
}

#: The equity account every opening balance is posted against. Its blind index
#: is a fixed string rather than a hash of the name: it is a system account, the
#: same one for everybody, and nothing ever searches for it by name.
OPENING_BALANCE_EQUITY_BLIND_INDEX = "system-opening-balances"
OPENING_BALANCE_EQUITY_NAME = "Opening balances"

_CODE_SUFFIX = re.compile(r"^\d+$")


def ensure_chart(user: Any) -> ChartOfAccounts:
    chart, _ = ChartOfAccounts.objects.get_or_create(
        user=user,
        name_blind_index=DEFAULT_CHART_BLIND_INDEX,
        defaults={"name_encrypted": DEFAULT_CHART_NAME},
    )
    return chart


def _next_code(chart: ChartOfAccounts, account_type: str) -> str:
    """The next free code in this type's range, allocated in tens.

    Tens rather than ones so a person inserting an account by hand later has
    somewhere to put it. The chart row is locked by the caller, and the unique
    constraint on ``(chart, code)`` is the backstop if it is not.
    """

    prefix = CODE_PREFIXES[account_type]
    used = [
        int(code)
        for code in LedgerAccount.objects.filter(chart=chart, code__startswith=prefix).values_list(
            "code", flat=True
        )
        if _CODE_SUFFIX.match(code)
    ]
    base = int(f"{prefix}000")
    return str(max([base - 10, *[code for code in used if code >= base]]) + 10)


@db_transaction.atomic
def _get_or_create_account(
    *,
    user: Any,
    account_type: str,
    name: str,
    name_blind_index: str,
    financial_account: FinancialAccount | None = None,
    is_system: bool = False,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> LedgerAccount:
    chart = ensure_chart(user)
    # Lock the chart so two concurrent creations cannot allocate one code twice.
    ChartOfAccounts.objects.select_for_update().get(pk=chart.pk)
    existing = LedgerAccount.objects.filter(
        chart=chart, parent=None, name_blind_index=name_blind_index
    ).first()
    if existing is not None:
        return existing

    account = LedgerAccount(
        user=user,
        chart=chart,
        code=_next_code(chart, account_type),
        name_encrypted=name,
        name_blind_index=name_blind_index,
        account_type=account_type,
        normal_balance=NORMAL_BALANCES[account_type],
        financial_account=financial_account,
        is_system=is_system,
    )
    if data_key is not None:
        account.encrypt_fields({"name_encrypted": name}, key=data_key, key_version=key_version)
    account.full_clean(validate_constraints=False)
    account.save()
    return account


def ensure_opening_balance_equity(
    user: Any, *, data_key: bytes | None = None, key_version: int = 1
) -> LedgerAccount:
    """The equity account that every opening balance is posted against.

    Opening balances need somewhere for the other half of the entry to go. It
    cannot be income — the money was not earned this year — and it cannot be an
    expense. Equity is what it is: the position the user brought with them.
    """

    return _get_or_create_account(
        user=user,
        account_type=LedgerAccount.AccountType.EQUITY,
        name=OPENING_BALANCE_EQUITY_NAME,
        name_blind_index=OPENING_BALANCE_EQUITY_BLIND_INDEX,
        is_system=True,
        data_key=data_key,
        key_version=key_version,
    )


def ensure_ledger_account_for(
    financial_account: FinancialAccount,
    *,
    name: str = "",
    data_key: bytes | None = None,
    key_version: int = 1,
) -> LedgerAccount:
    """The ledger account standing for one real account, created if absent."""

    ledger_type = FINANCIAL_ACCOUNT_LEDGER_TYPES.get(financial_account.account_type)
    if ledger_type is None:
        raise InvalidRequestError(f"'{financial_account.account_type}' has no ledger account type.")
    existing = LedgerAccount.objects.filter(financial_account=financial_account).first()
    if existing is not None:
        return existing
    return _get_or_create_account(
        user=financial_account.user,
        account_type=ledger_type,
        name=name or financial_account.name_blind_index,
        name_blind_index=financial_account.name_blind_index,
        financial_account=financial_account,
        data_key=data_key,
        key_version=key_version,
    )
