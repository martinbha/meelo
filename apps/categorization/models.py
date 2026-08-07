from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument


class Category(models.Model):
    class CategoryType(models.TextChoices):
        EXPENSE = "expense", "Expense"
        INCOME = "income", "Income"
        TRANSFER = "transfer", "Transfer"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        blank=True,
        null=True,
    )
    category_type = models.CharField(max_length=16, choices=CategoryType.choices)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "parent", "name_blind_index"),
                name="category_user_parent_name_blind_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "parent"), name="category_user_parent_idx"),
            models.Index(fields=("user", "category_type"), name="category_user_type_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.parent_id == self.id:
            errors["parent"] = "A category cannot be its own parent."
        if self.parent_id and self.user_id:
            parent_user_id = (
                Category.objects.filter(pk=self.parent_id).values_list("user_id", flat=True).first()
            )
            if parent_user_id != self.user_id:
                errors["parent"] = "A category parent must belong to the same user."

        ancestor = self.parent
        visited: set[uuid.UUID] = set()
        while ancestor is not None:
            if ancestor.pk in visited or ancestor.pk == self.pk:
                errors["parent"] = "Category parents must not contain a cycle."
                break
            visited.add(ancestor.pk)
            ancestor = ancestor.parent

        if errors:
            raise ValidationError(errors)

    def ancestors(self) -> list[Category]:
        result: list[Category] = []
        parent = self.parent
        while parent is not None:
            result.append(parent)
            parent = parent.parent
        return result

    def __str__(self) -> str:
        return self.name_blind_index


class MerchantAlias(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merchant_aliases",
    )
    alias_encrypted = models.TextField()
    alias_blind_index = models.CharField(max_length=128)
    normalized_merchant_encrypted = models.TextField()
    normalized_merchant_blind_index = models.CharField(max_length=128)
    default_category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="merchant_aliases",
        blank=True,
        null=True,
    )
    payment_instrument = models.ForeignKey(
        PaymentInstrument,
        on_delete=models.PROTECT,
        related_name="merchant_aliases",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("alias_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "alias_blind_index"),
                name="merchant_alias_user_alias_blind_unique",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.default_category_id:
            category_owner = (
                Category.objects.filter(pk=self.default_category_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if category_owner != self.user_id:
                errors["default_category"] = "The default category must belong to the same user."
        if self.payment_instrument_id:
            instrument_owner = (
                PaymentInstrument.objects.filter(pk=self.payment_instrument_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if instrument_owner != self.user_id:
                errors["payment_instrument"] = (
                    "The payment instrument must belong to the same user."
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.alias_blind_index


class CategoryRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="category_rules",
    )
    merchant_pattern_encrypted = models.TextField()
    merchant_pattern_blind_index = models.CharField(max_length=128)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="rules")
    payment_instrument = models.ForeignKey(
        PaymentInstrument,
        on_delete=models.PROTECT,
        related_name="category_rules",
        blank=True,
        null=True,
    )
    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.PROTECT,
        related_name="category_rules",
        blank=True,
        null=True,
    )
    amount_min_encrypted = models.TextField(blank=True)
    amount_max_encrypted = models.TextField(blank=True)
    priority = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-priority", "created_at")
        indexes = [
            models.Index(
                fields=("user", "merchant_pattern_blind_index", "is_active"),
                name="category_rule_lookup_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        related_objects: dict[str, tuple[Any, uuid.UUID | None]] = {
            "category": (Category, self.category_id),
            "payment_instrument": (PaymentInstrument, self.payment_instrument_id),
            "financial_account": (FinancialAccount, self.financial_account_id),
        }
        for field_name, (model, object_id) in related_objects.items():
            if object_id:
                owner = model.objects.filter(pk=object_id).values_list("user_id", flat=True).first()
                if owner != self.user_id:
                    errors[field_name] = "Related records must belong to the same user."
        if errors:
            raise ValidationError(errors)

    def matches(
        self,
        merchant_blind_index: str,
        *,
        payment_instrument_id: uuid.UUID | None = None,
        financial_account_id: uuid.UUID | None = None,
    ) -> bool:
        if not self.is_active or self.merchant_pattern_blind_index != merchant_blind_index:
            return False
        if self.payment_instrument_id and self.payment_instrument_id != payment_instrument_id:
            return False
        return not (self.financial_account_id and self.financial_account_id != financial_account_id)
