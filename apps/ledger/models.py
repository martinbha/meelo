from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.encrypted_fields import EncryptedFieldsMixin
from apps.financial_accounts.models import FinancialAccount


class ChartOfAccounts(EncryptedFieldsMixin, models.Model):
    encrypted_fields = ("name_encrypted",)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="charts_of_accounts",
    )
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "name_blind_index"),
                name="chart_user_name_blind_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name_blind_index


class LedgerAccount(EncryptedFieldsMixin, models.Model):
    encrypted_fields = ("name_encrypted",)

    class AccountType(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    class NormalBalance(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chart = models.ForeignKey(ChartOfAccounts, on_delete=models.CASCADE, related_name="accounts")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ledger_accounts",
    )
    code = models.CharField(max_length=32)
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    normal_balance = models.CharField(max_length=8, choices=NormalBalance.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        blank=True,
        null=True,
    )
    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.PROTECT,
        related_name="ledger_accounts",
        blank=True,
        null=True,
    )
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("chart", "code"), name="ledger_account_chart_code_unique"
            ),
            models.UniqueConstraint(
                fields=("chart", "parent", "name_blind_index"),
                name="ledger_account_chart_parent_name_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "account_type"), name="ledger_account_user_type_idx"),
            models.Index(fields=("chart", "parent"), name="ledger_acct_chart_parent_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        expected_normal: dict[str, str] = {
            self.AccountType.ASSET.value: self.NormalBalance.DEBIT.value,
            self.AccountType.EXPENSE.value: self.NormalBalance.DEBIT.value,
            self.AccountType.LIABILITY.value: self.NormalBalance.CREDIT.value,
            self.AccountType.EQUITY.value: self.NormalBalance.CREDIT.value,
            self.AccountType.INCOME.value: self.NormalBalance.CREDIT.value,
        }
        if expected_normal.get(self.account_type) != self.normal_balance:
            errors["normal_balance"] = "Normal balance does not match the ledger account type."

        if self.chart_id:
            chart_owner = (
                ChartOfAccounts.objects.filter(pk=self.chart_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if chart_owner != self.user_id:
                errors["chart"] = "The chart and ledger account must belong to the same user."
        if self.parent_id:
            parent = LedgerAccount.objects.filter(pk=self.parent_id).first()
            if parent is None or parent.user_id != self.user_id or parent.chart_id != self.chart_id:
                errors["parent"] = "A ledger parent must belong to the same chart and user."
            ancestor = parent
            visited: set[uuid.UUID] = set()
            while ancestor is not None:
                if ancestor.pk in visited or ancestor.pk == self.pk:
                    errors["parent"] = "Ledger account parents must not contain a cycle."
                    break
                visited.add(ancestor.pk)
                ancestor = ancestor.parent
        if self.financial_account_id:
            account_owner = (
                FinancialAccount.objects.filter(pk=self.financial_account_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if account_owner != self.user_id:
                errors["financial_account"] = "The financial account must belong to the same user."
        if errors:
            raise ValidationError(errors)

    def ancestors(self) -> list[LedgerAccount]:
        result: list[LedgerAccount] = []
        parent = self.parent
        while parent is not None:
            result.append(parent)
            parent = parent.parent
        return result

    def __str__(self) -> str:
        return f"{self.code} {self.name_blind_index}"


class LedgerEntry(EncryptedFieldsMixin, models.Model):
    encrypted_fields = ("amount_encrypted",)

    class EntryType(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        "transactions.CanonicalTransaction",
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    entry_type = models.CharField(max_length=8, choices=EntryType.choices)
    amount_encrypted = models.TextField()
    currency = models.CharField(max_length=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("transaction", "entry_type"), name="ledger_entry_txn_type_idx"),
            models.Index(fields=("account", "created_at"), name="ledger_entry_account_date_idx"),
        ]

    @property
    def encryption_owner_id(self) -> Any:
        """A ledger entry belongs to whoever owns its transaction.

        The entry carries no owner column of its own — it is reached through the
        transaction, and duplicating the owner would create two places for one
        fact to be wrong. The associated data still binds the ciphertext to a
        person, so an entry moved to another user's transaction fails to open
        rather than reporting their money as this one's.
        """

        return self.transaction.user_id

    def __str__(self) -> str:
        return f"{self.entry_type} {self.currency}"
