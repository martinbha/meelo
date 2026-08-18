from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.currencies import is_supported, normalize_code


class FinancialAccount(models.Model):
    class AccountType(models.TextChoices):
        CHECKING = "checking", "Checking"
        SAVINGS = "savings", "Savings"
        CASH = "cash", "Cash"
        CREDIT_CARD_LIABILITY = "credit_card_liability", "Credit-card liability"
        LOAN = "loan", "Loan"
        INVESTMENT = "investment", "Investment"
        OTHER_ASSET = "other_asset", "Other asset"
        OTHER_LIABILITY = "other_liability", "Other liability"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="financial_accounts",
    )
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    institution_encrypted = models.TextField()
    institution_blind_index = models.CharField(max_length=128)
    account_type = models.CharField(max_length=32, choices=AccountType.choices)
    masked_identifier_encrypted = models.TextField(blank=True)
    identifier_last_four = models.CharField(max_length=4, blank=True)
    currency = models.CharField(max_length=3, default="KRW")
    opening_balance_encrypted = models.TextField(default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "name_blind_index"),
                name="financial_account_user_name_blind_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    account_type__in=(
                        "checking",
                        "savings",
                        "cash",
                        "credit_card_liability",
                        "loan",
                        "investment",
                        "other_asset",
                        "other_liability",
                    )
                ),
                name="financial_account_type_valid",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "is_active"), name="financial_account_active_idx"),
            models.Index(fields=("user", "account_type"), name="financial_account_type_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        self.currency = normalize_code(self.currency)
        if not is_supported(self.currency):
            raise ValidationError(
                {"currency": f"'{self.currency}' is not a currency this application supports."}
            )
        if self.identifier_last_four and len(self.identifier_last_four) != 4:
            raise ValidationError(
                {"identifier_last_four": "The identifier suffix must be four characters."}
            )

    def __str__(self) -> str:
        return f"{self.name_blind_index} ({self.account_type})"
