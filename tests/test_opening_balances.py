"""Where an account started, and why it is not income (#155, specification 6.2, 7, 25.2).

An account opened with money already in it has to say so, or every balance the
ledger derives is short by that amount until the user stops trusting the figure.
Posting it as income would be the easy way to get the balance right and would
report a payday that never happened.

So the two claims here are in tension and both are tested: the opening balance
*is* in the balance, and it is *not* in any report.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest

from apps.core.errors import ForbiddenError, InvalidRequestError
from apps.core.value_objects import Money
from apps.financial_accounts.models import FinancialAccount
from apps.financial_accounts.opening_balances import (
    correct_opening_balance,
    post_opening_balance,
    posted_opening_balance_minor,
)
from apps.ledger.balances import account_balances, financial_account_balances, positions
from apps.ledger.chart import (
    OPENING_BALANCE_EQUITY_BLIND_INDEX,
    ensure_ledger_account_for,
    ensure_opening_balance_equity,
)
from apps.ledger.models import LedgerAccount, LedgerEntry
from apps.reports.spending import monthly_spending, reportable_transactions
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db



@pytest.fixture
def owner() -> Any:
    return make_user(email="opening@example.com")


def account_with(
    user: Any, minor: int | None, *, account_type: str = "checking", suffix: str = "a"
) -> FinancialAccount:
    return make_account(
        user,
        name_blind_index=f"opening-{suffix}",
        account_type=account_type,
        opening_balance_encrypted=f"{minor}:KRW" if minor is not None else "",
    )


def balance_of(user: Any, account: FinancialAccount) -> int:
    return financial_account_balances(user).get(account.pk, {}).get("KRW", 0)


# ----------------------------------------------------------------------
# The balance is right from day one
# ----------------------------------------------------------------------


def test_an_account_reports_its_opening_balance_before_any_transaction(owner: Any) -> None:
    account = account_with(owner, 2_400_000)

    result = post_opening_balance(account, user=owner)

    assert result.posted
    assert result.signed_minor == 2_400_000
    assert balance_of(owner, account) == 2_400_000
    assert positions(owner)["KRW"].assets_minor == 2_400_000


def test_a_card_opened_with_a_debt_owes_rather_than_holds(owner: Any) -> None:
    """The liability's sign is the thing that goes wrong if entries are signed alike."""

    card = account_with(owner, 380_000, account_type="credit_card_liability", suffix="card")

    post_opening_balance(card, user=owner)

    assert balance_of(owner, card) == 380_000
    position = positions(owner)["KRW"]
    assert position.liabilities_minor == 380_000
    assert position.assets_minor == 0
    assert position.net_minor == -380_000


def test_an_overdrawn_account_opens_negative(owner: Any) -> None:
    account = account_with(owner, -50_000, suffix="overdrawn")

    post_opening_balance(account, user=owner)

    assert balance_of(owner, account) == -50_000


def test_the_posting_is_balanced_against_equity(owner: Any) -> None:
    account = account_with(owner, 1_000_000)

    post_opening_balance(account, user=owner)

    equity = LedgerAccount.objects.get(name_blind_index=OPENING_BALANCE_EQUITY_BLIND_INDEX)
    equity_balance = next(b for b in account_balances(owner) if b.account_id == equity.pk)
    assert equity.account_type == LedgerAccount.AccountType.EQUITY
    assert equity_balance.amount_minor == 1_000_000
    assert LedgerEntry.objects.count() == 2


def test_an_account_with_no_opening_balance_posts_nothing(owner: Any) -> None:
    """Two rows saying zero would make every account look like it began with an event."""

    for minor in (0, None):
        account = account_with(owner, minor, suffix=f"zero-{minor}")
        result = post_opening_balance(account, user=owner)
        assert not result.posted

    assert LedgerEntry.objects.count() == 0
    assert CanonicalTransaction.objects.count() == 0


def test_posting_the_same_opening_balance_twice_is_refused(owner: Any) -> None:
    account = account_with(owner, 500_000)
    post_opening_balance(account, user=owner)

    with pytest.raises(InvalidRequestError, match="already has an opening balance"):
        post_opening_balance(account, user=owner)

    assert balance_of(owner, account) == 500_000


# ----------------------------------------------------------------------
# And it is not income, and not spending
# ----------------------------------------------------------------------


def test_an_opening_balance_is_neither_income_nor_spending(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner, occurred_at=date(2026, 8, 1))

    totals = monthly_spending(owner, year=2026, month=8).totals("KRW")

    assert totals.income_minor == 0
    assert totals.gross_spending_minor == 0
    assert totals.neutral_minor == 0
    assert totals.unresolved_minor == 0
    assert totals.transaction_count == 0


def test_an_opening_balance_never_reaches_a_report_at_all(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner, occurred_at=date(2026, 8, 1))

    assert CanonicalTransaction.objects.filter(user=owner).count() == 1
    assert list(reportable_transactions(owner)) == []


# ----------------------------------------------------------------------
# Corrections are adjustments
# ----------------------------------------------------------------------


def test_correcting_an_opening_balance_posts_the_difference(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    original = post_opening_balance(account, user=owner)

    result = correct_opening_balance(
        account,
        user=owner,
        corrected=Money(2_500_000, "KRW"),
        reason="Misread the last digit on the statement.",
    )

    assert result.is_adjustment
    assert result.signed_minor == 100_000
    assert balance_of(owner, account) == 2_500_000
    # The original posting is untouched; the correction is a further pair.
    assert LedgerEntry.objects.filter(transaction=original.transaction).count() == 2
    assert LedgerEntry.objects.count() == 4
    assert posted_opening_balance_minor(account) == 2_500_000


def test_a_downward_correction_posts_the_difference_the_other_way(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner)

    correct_opening_balance(
        account, user=owner, corrected=Money(2_000_000, "KRW"), reason="Wrong account."
    )

    assert balance_of(owner, account) == 2_000_000


def test_a_correction_to_the_same_figure_posts_nothing(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner)

    result = correct_opening_balance(
        account, user=owner, corrected=Money(2_400_000, "KRW"), reason="Checked again."
    )

    assert not result.posted
    assert LedgerEntry.objects.count() == 2


def test_a_correction_requires_a_reason(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner)

    with pytest.raises(InvalidRequestError, match="requires a reason"):
        correct_opening_balance(account, user=owner, corrected=Money(1, "KRW"), reason=" ")

    assert balance_of(owner, account) == 2_400_000


def test_a_correction_in_another_currency_is_refused(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner)

    with pytest.raises(InvalidRequestError, match="currency"):
        correct_opening_balance(
            account, user=owner, corrected=Money(1_000, "USD"), reason="Wrong currency."
        )


def test_corrections_are_audited_without_the_figure(owner: Any) -> None:
    account = account_with(owner, 2_400_000)
    post_opening_balance(account, user=owner)
    correct_opening_balance(
        account, user=owner, corrected=Money(2_500_000, "KRW"), reason="Statement reread."
    )

    posted = owner.audit_events.filter(event_type="opening_balance_posted").get()
    adjusted = owner.audit_events.filter(event_type="opening_balance_adjusted").get()

    assert posted.metadata["currency"] == "KRW"
    assert adjusted.metadata["reason"] == "Statement reread."
    for event in (posted, adjusted):
        assert "2400000" not in str(event.metadata)
        assert "2500000" not in str(event.metadata)


# ----------------------------------------------------------------------
# Ownership and the chart
# ----------------------------------------------------------------------


def test_an_opening_balance_cannot_be_posted_for_another_users_account(owner: Any) -> None:
    intruder = make_user(email="opening-intruder@example.com")
    account = account_with(owner, 2_400_000)

    with pytest.raises(ForbiddenError):
        post_opening_balance(account, user=intruder)
    with pytest.raises(ForbiddenError):
        correct_opening_balance(
            account, user=intruder, corrected=Money(1, "KRW"), reason="Mine now."
        )

    assert LedgerEntry.objects.count() == 0


def test_each_user_gets_their_own_equity_account(owner: Any) -> None:
    stranger = make_user(email="opening-stranger@example.com")

    mine = ensure_opening_balance_equity(owner)
    theirs = ensure_opening_balance_equity(stranger)

    assert mine.pk != theirs.pk
    assert mine.user_id == owner.pk
    assert theirs.user_id == stranger.pk
    # And asking twice does not make a third.
    assert ensure_opening_balance_equity(owner).pk == mine.pk


def test_ledger_accounts_are_created_once_with_the_right_normal_balance(owner: Any) -> None:
    checking = account_with(owner, 0, suffix="checking")
    card = account_with(owner, 0, account_type="credit_card_liability", suffix="card")

    asset = ensure_ledger_account_for(checking)
    liability = ensure_ledger_account_for(card)

    assert asset.account_type == LedgerAccount.AccountType.ASSET
    assert asset.normal_balance == LedgerAccount.NormalBalance.DEBIT
    assert liability.account_type == LedgerAccount.AccountType.LIABILITY
    assert liability.normal_balance == LedgerAccount.NormalBalance.CREDIT
    assert asset.code != liability.code
    assert ensure_ledger_account_for(checking).pk == asset.pk


def test_the_ledger_account_name_is_encrypted_under_its_own_identity(owner: Any) -> None:
    """A ciphertext copied from the financial account would never open again."""

    from apps.core.crypto import decrypt_model_field

    key = os.urandom(32)
    checking = account_with(owner, 0, suffix="named")

    ledger_account = ensure_ledger_account_for(checking, name="Shinhan checking", data_key=key)

    assert ledger_account.name_encrypted != "Shinhan checking"
    assert decrypt_model_field(ledger_account, "name_encrypted", key=key) == "Shinhan checking"
