"""Balances derived from the ledger, checked against hand-computed money (#154).

The figures in these tests come from specification section 7, which writes each
ledger rule out as two lines of arithmetic. They are not read back from anything
the code produced: a balance test whose expected value came from the balance
code proves only that it is consistent with itself.

The case worth stating is the credit card. A payment against it is a debit, and
a debit increases a bank account — so signing every entry the same way would
report a card balance that *grows* as it is paid off. Each account's normal
balance decides its sign, and the tests below check both directions rather than
assuming the asset case generalises.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from apps.core.value_objects import Money
from apps.ledger.balances import (
    NORMAL_BALANCE_SIGNS,
    account_balances,
    financial_account_balances,
    positions,
)
from apps.ledger.models import ChartOfAccounts, LedgerAccount, LedgerEntry
from apps.ledger.posting import Posting, post_balanced_transaction
from apps.ledger.rules import PostingRuleAccounts, post_transaction_by_type
from apps.transactions.deletion import delete_transaction
from apps.transactions.lifecycle import transition_transaction_status
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_transaction, make_user

pytestmark = pytest.mark.django_db

CONFIRMED = CanonicalTransaction.Status.CONFIRMED


class Books:
    """One user's chart of accounts, built the way the fixtures describe it."""

    def __init__(self, user: Any) -> None:
        self.user = user
        self.chart = ChartOfAccounts.objects.create(
            user=user, name_encrypted="personal", name_blind_index=f"chart-{user.pk}"
        )
        self.bank_account = make_account(user, name_blind_index=f"bank-{user.pk}")
        self.savings_account = make_account(user, name_blind_index=f"savings-{user.pk}")
        self.cash_account = make_account(
            user,
            name_blind_index=f"cash-{user.pk}",
            account_type="cash",
        )
        self.card_account = make_account(
            user,
            name_blind_index=f"card-{user.pk}",
            account_type="credit_card_liability",
        )
        self.bank = self._ledger("1000", "asset", "debit", self.bank_account)
        self.savings = self._ledger("1010", "asset", "debit", self.savings_account)
        self.cash = self._ledger("1020", "asset", "debit", self.cash_account)
        self.card = self._ledger("2000", "liability", "credit", self.card_account)
        self.food = self._ledger("5000", "expense", "debit", None)

    def _ledger(
        self, code: str, account_type: str, normal_balance: str, financial_account: Any
    ) -> LedgerAccount:
        return LedgerAccount.objects.create(
            user=self.user,
            chart=self.chart,
            code=code,
            name_encrypted=code,
            name_blind_index=f"{code}-{self.user.pk}",
            account_type=account_type,
            normal_balance=normal_balance,
            financial_account=financial_account,
        )

    def post(
        self,
        transaction_type: str,
        minor: int,
        *,
        account: Any,
        context: PostingRuleAccounts,
        occurred_at: date = date(2026, 8, 7),
    ) -> CanonicalTransaction:
        transaction = make_transaction(
            self.user,
            account,
            amount_encrypted=f"{minor}:KRW",
            transaction_type=transaction_type,
            occurred_at=occurred_at,
        )
        transaction = transition_transaction_status(
            transaction.pk, user=self.user, status=CONFIRMED
        )
        post_transaction_by_type(transaction, context)
        return transaction


@pytest.fixture
def books() -> Books:
    return Books(make_user(email="balances@example.com"))


def balance_of(user: Any, code: str) -> int:
    for balance in account_balances(user):
        if balance.code == code:
            return balance.amount_minor
    return 0


# ----------------------------------------------------------------------
# The rules in specification 7, one at a time
# ----------------------------------------------------------------------


def test_a_debit_card_purchase_moves_thirty_thousand_out_of_the_bank(books: Books) -> None:
    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )

    assert balance_of(books.user, "1000") == -30_000
    assert balance_of(books.user, "5000") == 30_000


def test_a_credit_card_purchase_increases_what_is_owed(books: Books) -> None:
    """The liability grows. It does not go negative, which is the bug this catches."""

    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.card_account,
        context=PostingRuleAccounts(account=books.card, offset=books.food),
    )

    assert balance_of(books.user, "2000") == 30_000
    assert balance_of(books.user, "5000") == 30_000


def test_a_card_payment_reduces_the_debt_and_the_bank(books: Books) -> None:
    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        800_000,
        account=books.card_account,
        context=PostingRuleAccounts(account=books.card, offset=books.food),
    )
    books.post(
        CanonicalTransaction.TransactionType.CREDIT_CARD_PAYMENT,
        800_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, liability_account=books.card),
    )

    assert balance_of(books.user, "1000") == -800_000
    assert balance_of(books.user, "2000") == 0


def test_an_internal_transfer_moves_money_without_creating_or_destroying_it(
    books: Books,
) -> None:
    books.post(
        CanonicalTransaction.TransactionType.INTERNAL_TRANSFER,
        500_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, transfer_account=books.savings),
    )

    assert balance_of(books.user, "1000") == -500_000
    assert balance_of(books.user, "1010") == 500_000
    assert positions(books.user)["KRW"].net_minor == 0


def test_a_refund_returns_money_and_reduces_the_expense(books: Books) -> None:
    books.post(
        CanonicalTransaction.TransactionType.REFUND,
        42_900,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )

    assert balance_of(books.user, "1000") == 42_900
    assert balance_of(books.user, "5000") == -42_900


def test_a_cash_withdrawal_is_movement_rather_than_spending(books: Books) -> None:
    books.post(
        CanonicalTransaction.TransactionType.CASH_WITHDRAWAL,
        100_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, transfer_account=books.cash),
    )

    assert balance_of(books.user, "1000") == -100_000
    assert balance_of(books.user, "1020") == 100_000
    assert positions(books.user)["KRW"].net_minor == 0


# ----------------------------------------------------------------------
# A month of them together
# ----------------------------------------------------------------------


def test_a_whole_month_adds_up_to_the_hand_computed_position(books: Books) -> None:
    """Six events, arithmetic done on paper first.

    bank:    -30,000 -800,000 -500,000 +42,900 -100,000  =   -1,387,100
    savings:                  +500,000                    =      500,000
    cash:                                        +100,000 =      100,000
    card:    +30,000 -800,000                             =     -770,000
    assets = -1,387,100 + 500,000 + 100,000               =     -787,100
    net    = assets - liabilities = -787,100 - (-770,000) =      -17,100
    """

    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.card_account,
        context=PostingRuleAccounts(account=books.card, offset=books.food),
    )
    books.post(
        CanonicalTransaction.TransactionType.CREDIT_CARD_PAYMENT,
        800_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, liability_account=books.card),
    )
    books.post(
        CanonicalTransaction.TransactionType.INTERNAL_TRANSFER,
        500_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, transfer_account=books.savings),
    )
    books.post(
        CanonicalTransaction.TransactionType.REFUND,
        42_900,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    books.post(
        CanonicalTransaction.TransactionType.CASH_WITHDRAWAL,
        100_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, transfer_account=books.cash),
    )

    assert balance_of(books.user, "1000") == -1_387_100
    assert balance_of(books.user, "1010") == 500_000
    assert balance_of(books.user, "1020") == 100_000
    assert balance_of(books.user, "2000") == -770_000

    position = positions(books.user)["KRW"]
    assert position.assets_minor == -787_100
    assert position.liabilities_minor == -770_000
    assert position.net_minor == -17_100
    assert position.net == Money(-17_100, "KRW")


# ----------------------------------------------------------------------
# Arithmetic, statuses, currencies, ownership
# ----------------------------------------------------------------------


def test_every_account_type_has_a_sign_for_both_entry_types() -> None:
    assert set(NORMAL_BALANCE_SIGNS) == set(LedgerAccount.NormalBalance.values)
    for signs in NORMAL_BALANCE_SIGNS.values():
        assert set(signs) == set(LedgerEntry.EntryType.values)
        assert sorted(signs.values()) == [-1, 1]


def test_balances_are_integers_all_the_way_through(books: Books) -> None:
    """Specification 15.1: money never touches binary floating point."""

    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        33_333,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )

    for balance in account_balances(books.user):
        assert isinstance(balance.amount_minor, int)
        assert not isinstance(balance.amount_minor, bool)
    assert isinstance(positions(books.user)["KRW"].net_minor, int)


def test_a_deleted_transaction_leaves_the_balance(books: Books) -> None:
    kept = books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    removed = books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        99_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    assert balance_of(books.user, "1000") == -129_000

    delete_transaction(removed.pk, user=books.user, confirmed=True)

    assert balance_of(books.user, "1000") == -30_000
    assert kept.status == CONFIRMED


def test_a_draft_transaction_contributes_nothing(books: Books) -> None:
    """A proposal is not money. It has no entries, and the balance says so."""

    make_transaction(books.user, books.bank_account, amount_encrypted="55000:KRW")

    assert balance_of(books.user, "1000") == 0
    assert positions(books.user) == {}


def test_two_currencies_are_reported_apart_rather_than_added(books: Books) -> None:
    krw = books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    assert krw.currency == "KRW"

    usd = make_transaction(
        books.user,
        books.bank_account,
        amount_encrypted="2500:USD",
        currency="USD",
    )
    usd = transition_transaction_status(usd.pk, user=books.user, status=CONFIRMED)
    post_balanced_transaction(
        usd,
        [
            Posting(books.food, LedgerEntry.EntryType.DEBIT, Money(2_500, "USD")),
            Posting(books.bank, LedgerEntry.EntryType.CREDIT, Money(2_500, "USD")),
        ],
    )

    bank = {b.currency: b.amount_minor for b in account_balances(books.user) if b.code == "1000"}
    assert bank == {"KRW": -30_000, "USD": -2_500}
    assert positions(books.user)["KRW"].assets_minor == -30_000
    assert positions(books.user)["USD"].assets_minor == -2_500


def test_balances_are_keyed_by_the_account_a_person_recognises(books: Books) -> None:
    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )

    by_account = financial_account_balances(books.user)

    assert by_account[books.bank_account.pk] == {"KRW": -30_000}
    # The expense account stands for nothing a person opened, so it is absent.
    assert len(by_account) == 1


def test_one_users_ledger_never_reaches_another(books: Books) -> None:
    stranger = Books(make_user(email="balances-stranger@example.com"))
    books.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        30_000,
        account=books.bank_account,
        context=PostingRuleAccounts(account=books.bank, offset=books.food),
    )
    stranger.post(
        CanonicalTransaction.TransactionType.PURCHASE,
        777_000,
        account=stranger.bank_account,
        context=PostingRuleAccounts(account=stranger.bank, offset=stranger.food),
    )

    assert balance_of(books.user, "1000") == -30_000
    assert balance_of(stranger.user, "1000") == -777_000
    assert positions(books.user)["KRW"].assets_minor == -30_000
    assert positions(stranger.user)["KRW"].assets_minor == -777_000
    assert books.bank_account.pk not in financial_account_balances(stranger.user)
