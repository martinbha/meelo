from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.core.encrypted_fields import EncryptedFieldsMixin
from apps.financial_accounts.models import FinancialAccount

from .validators import validate_payment_instrument_mapping


class PaymentInstrument(EncryptedFieldsMixin, models.Model):
    encrypted_fields = ("name_encrypted", "issuer_encrypted")

    class InstrumentType(models.TextChoices):
        DEBIT_CARD = "debit_card", "Debit card"
        CREDIT_CARD = "credit_card", "Credit card"
        VIRTUAL_CARD = "virtual_card", "Virtual card"
        PREPAID_CARD = "prepaid_card", "Prepaid card"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payment_instruments",
    )
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    instrument_type = models.CharField(max_length=24, choices=InstrumentType.choices)
    last_four = models.CharField(max_length=4, blank=True)
    financial_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.PROTECT,
        related_name="payment_instruments",
    )
    settlement_account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.PROTECT,
        related_name="settlement_instruments",
        blank=True,
        null=True,
    )
    issuer_encrypted = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "name_blind_index"),
                name="instrument_user_name_blind_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "is_active"), name="instrument_user_active_idx"),
            models.Index(fields=("user", "instrument_type"), name="instrument_user_type_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        validate_payment_instrument_mapping(self)

    def __str__(self) -> str:
        return f"{self.name_blind_index} ({self.instrument_type})"
