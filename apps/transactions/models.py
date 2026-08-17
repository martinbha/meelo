from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.categorization.models import Category
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.users.models import User


class CanonicalTransaction(models.Model):
    class TransactionType(models.TextChoices):
        PURCHASE = "purchase", "Purchase"
        INCOME = "income", "Income"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        INTERNAL_TRANSFER = "internal_transfer", "Internal transfer"
        CREDIT_CARD_PAYMENT = "credit_card_payment", "Credit-card payment"
        CASH_WITHDRAWAL = "cash_withdrawal", "Cash withdrawal"
        REFUND = "refund", "Refund"
        FEE = "fee", "Fee"
        INTEREST = "interest", "Interest"
        LOAN_PAYMENT = "loan_payment", "Loan payment"
        ADJUSTMENT = "adjustment", "Adjustment"
        UNKNOWN = "unknown", "Unknown"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMED = "confirmed", "Confirmed"
        VOIDED = "voided", "Voided"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="canonical_transactions",
    )
    transaction_type = models.CharField(
        max_length=32,
        choices=TransactionType.choices,
        default=TransactionType.UNKNOWN,
    )
    occurred_at = models.DateField()
    posted_at = models.DateField(blank=True, null=True)
    merchant_encrypted = models.TextField(blank=True)
    merchant_blind_index = models.CharField(max_length=128, blank=True)
    counterparty_encrypted = models.TextField(blank=True)
    counterparty_blind_index = models.CharField(max_length=128, blank=True)
    amount_encrypted = models.TextField()
    currency = models.CharField(max_length=3, default="KRW")
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="canonical_transactions",
        blank=True,
        null=True,
    )
    #: What decided the category. Stored rather than recomputed, because it is
    #: what stops re-classification from overwriting a correction the user made
    #: — and because a category nobody can explain cannot be argued with.
    #: Values come from :class:`apps.categorization.engine.CategorySource`;
    #: the field is a plain CharField so the transactions app does not have to
    #: depend on the categorization app to define its own schema.
    category_source = models.CharField(max_length=32, default="uncategorized")
    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.PROTECT,
        related_name="canonical_transactions",
    )
    payment_instrument = models.ForeignKey(
        PaymentInstrument,
        on_delete=models.PROTECT,
        related_name="canonical_transactions",
        blank=True,
        null=True,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    #: Deterministic key of whatever produced this transaction, so a retried
    #: worker or a double-clicked button converges on one row instead of
    #: creating a second. Blank for manual entry, which has no natural origin —
    #: two identical manual entries are a legitimate thing for a person to make.
    #: Stored in clear, so it carries an origin and an identifier and nothing
    #: a database reader should not learn.
    source_idempotency_key = models.CharField(max_length=128, blank=True)
    notes_encrypted = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_canonical_transactions",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_canonical_transactions",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-occurred_at", "-created_at")
        constraints = [
            # The backstop behind the row locks: even a caller that never took
            # one cannot produce a second transaction for the same origin.
            models.UniqueConstraint(
                fields=("user", "source_idempotency_key"),
                condition=~models.Q(source_idempotency_key=""),
                name="transaction_source_idempotency_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "occurred_at"), name="transaction_user_date_idx"),
            models.Index(fields=("user", "status"), name="transaction_user_status_idx"),
            models.Index(fields=("user", "merchant_blind_index"), name="transaction_merchant_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.posted_at and self.posted_at < self.occurred_at:
            errors["posted_at"] = "Posted date cannot be earlier than the occurred date."
        self.currency = self.currency.upper()
        if len(self.currency) != 3 or not self.currency.isalpha():
            errors["currency"] = "Currency must be a three-letter code."

        related_objects: dict[str, tuple[Any, Any]] = {
            "category": (Category, self.category_id),
            "financial_account": (FinancialAccount, self.financial_account_id),
            "payment_instrument": (PaymentInstrument, self.payment_instrument_id),
            "created_by": (User, self.created_by_id),
        }
        if self.reviewed_by_id:
            related_objects["reviewed_by"] = (User, self.reviewed_by_id)
        for field_name, (model, object_id) in related_objects.items():
            if object_id and field_name not in {"created_by", "reviewed_by"}:
                owner = model.objects.filter(pk=object_id).values_list("user_id", flat=True).first()
                if owner != self.user_id:
                    errors[field_name] = "Related records must belong to the same user."
            elif object_id and field_name in {"created_by", "reviewed_by"}:
                if not model.objects.filter(pk=object_id).exists():
                    errors[field_name] = "The referenced user does not exist."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.occurred_at}: {self.transaction_type}"
