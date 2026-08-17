from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apps.core.errors import InvalidRequestError
from apps.core.value_objects import Currency, Money
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money

from .models import LedgerAccount, LedgerEntry
from .posting import Posting, post_balanced_transaction


@dataclass(frozen=True, slots=True)
class PostingRuleAccounts:
    """Ledger accounts required by a transaction-type rule.

    ``account`` is the account represented by the canonical transaction.
    ``offset`` is normally an expense, income, cash, or liability account.
    ``transfer_account`` is the destination for transfers. Card payments use
    ``liability_account`` for the card liability being paid down.
    """

    account: LedgerAccount
    offset: LedgerAccount | None = None
    transfer_account: LedgerAccount | None = None
    liability_account: LedgerAccount | None = None


def _amount_for(transaction: CanonicalTransaction, *, data_key: bytes | None = None) -> Money:
    amount = read_money(transaction, "amount_encrypted", data_key=data_key)
    if amount.resolved_currency != Currency(transaction.currency):
        raise InvalidRequestError(
            "Encoded transaction amount currency does not match the transaction."
        )
    if amount.amount_minor <= 0:
        raise InvalidRequestError("Transaction amounts must be positive for ledger posting.")
    return amount


def _require_offset(context: PostingRuleAccounts) -> LedgerAccount:
    if context.offset is None:
        raise InvalidRequestError("This transaction type requires an offset ledger account.")
    return context.offset


def _require_transfer(context: PostingRuleAccounts) -> LedgerAccount:
    if context.transfer_account is None:
        raise InvalidRequestError("Transfer transactions require a destination ledger account.")
    return context.transfer_account


def _require_liability(context: PostingRuleAccounts) -> LedgerAccount:
    if context.liability_account is None:
        raise InvalidRequestError("Card payments require a liability ledger account.")
    return context.liability_account


def _purchase(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(_require_offset(context), LedgerEntry.EntryType.DEBIT, amount),
        Posting(context.account, LedgerEntry.EntryType.CREDIT, amount),
    ]


def _income(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(context.account, LedgerEntry.EntryType.DEBIT, amount),
        Posting(_require_offset(context), LedgerEntry.EntryType.CREDIT, amount),
    ]


def _transfer(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(_require_transfer(context), LedgerEntry.EntryType.DEBIT, amount),
        Posting(context.account, LedgerEntry.EntryType.CREDIT, amount),
    ]


def _card_payment(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(_require_liability(context), LedgerEntry.EntryType.DEBIT, amount),
        Posting(context.account, LedgerEntry.EntryType.CREDIT, amount),
    ]


def _refund(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(context.account, LedgerEntry.EntryType.DEBIT, amount),
        Posting(_require_offset(context), LedgerEntry.EntryType.CREDIT, amount),
    ]


def _debit_expense(amount: Money, context: PostingRuleAccounts) -> list[Posting]:
    return [
        Posting(_require_offset(context), LedgerEntry.EntryType.DEBIT, amount),
        Posting(context.account, LedgerEntry.EntryType.CREDIT, amount),
    ]


RuleHandler = Callable[[Money, PostingRuleAccounts], list[Posting]]
POSTING_RULES: dict[str, RuleHandler] = {
    CanonicalTransaction.TransactionType.PURCHASE: _purchase,
    CanonicalTransaction.TransactionType.INCOME: _income,
    CanonicalTransaction.TransactionType.BANK_TRANSFER: _transfer,
    CanonicalTransaction.TransactionType.INTERNAL_TRANSFER: _transfer,
    CanonicalTransaction.TransactionType.CREDIT_CARD_PAYMENT: _card_payment,
    CanonicalTransaction.TransactionType.CASH_WITHDRAWAL: _transfer,
    CanonicalTransaction.TransactionType.REFUND: _refund,
    CanonicalTransaction.TransactionType.FEE: _debit_expense,
    CanonicalTransaction.TransactionType.INTEREST: _debit_expense,
    CanonicalTransaction.TransactionType.LOAN_PAYMENT: _card_payment,
    CanonicalTransaction.TransactionType.ADJUSTMENT: _debit_expense,
}


def build_transaction_postings(
    canonical_transaction: CanonicalTransaction,
    context: PostingRuleAccounts,
    *,
    data_key: bytes | None = None,
) -> list[Posting]:
    try:
        handler = POSTING_RULES[canonical_transaction.transaction_type]
    except KeyError as exc:
        raise InvalidRequestError(
            f"No ledger posting rule exists for '{canonical_transaction.transaction_type}'."
        ) from exc

    if context.account.financial_account_id != canonical_transaction.financial_account_id:
        raise InvalidRequestError(
            "The primary ledger account must link to the transaction financial account."
        )
    amount = _amount_for(canonical_transaction, data_key=data_key)
    return handler(amount, context)


def post_transaction_by_type(
    canonical_transaction: CanonicalTransaction,
    context: PostingRuleAccounts,
    *,
    data_key: bytes | None = None,
    key_version: int = 1,
) -> list[LedgerEntry]:
    return post_balanced_transaction(
        canonical_transaction,
        build_transaction_postings(canonical_transaction, context, data_key=data_key),
        data_key=data_key,
        key_version=key_version,
    )
