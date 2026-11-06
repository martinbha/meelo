from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from django import forms

from apps.categorization.models import Category
from apps.core.blind_index import SearchKey
from apps.core.crypto import is_encrypted_value
from apps.core.currencies import UnknownCurrencyError, definition_for, normalize_code
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument

from .models import CanonicalTransaction
from .services import create_manual_transaction, update_manual_transaction


class ManualTransactionForm(forms.Form):
    occurred_at = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(
        min_value=Decimal("0"),
        max_digits=18,
        # The currency is another form field. Validate its exponent in clean()
        # without reserving decimal places and reducing the whole-digit limit.
        decimal_places=None,
        help_text="Enter the amount in the selected currency's units.",
        widget=forms.NumberInput(attrs={"step": "any"}),
    )
    currency = forms.CharField(max_length=3, initial="KRW")
    transaction_type = forms.ChoiceField(choices=CanonicalTransaction.TransactionType.choices)
    financial_account = forms.ModelChoiceField(queryset=FinancialAccount.objects.none())
    payment_instrument = forms.ModelChoiceField(
        queryset=PaymentInstrument.objects.none(), required=False
    )
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    merchant = forms.CharField(max_length=500, required=False)
    counterparty = forms.CharField(max_length=500, required=False)
    notes = forms.CharField(widget=forms.Textarea, required=False)

    def __init__(
        self, *, user: Any, instance: CanonicalTransaction | None = None, **kwargs: Any
    ) -> None:
        self.user = user
        self.instance = instance
        super().__init__(**kwargs)
        self.fields["financial_account"].queryset = FinancialAccount.objects.filter(  # type: ignore[attr-defined]
            user=user, is_active=True
        )
        self.fields["payment_instrument"].queryset = PaymentInstrument.objects.filter(  # type: ignore[attr-defined]
            user=user, is_active=True
        )
        self.fields["category"].queryset = Category.objects.filter(user=user)  # type: ignore[attr-defined]
        if instance is not None and not kwargs.get("data"):
            self.initial.update(
                occurred_at=instance.occurred_at,
                amount=self._initial_amount(instance),
                currency=instance.currency,
                transaction_type=instance.transaction_type,
                financial_account=instance.financial_account_id,
                payment_instrument=instance.payment_instrument_id,
                category=instance.category_id,
                merchant=instance.merchant_encrypted,
                counterparty=instance.counterparty_encrypted,
                notes=instance.notes_encrypted,
            )

    def clean_currency(self) -> str:
        currency = normalize_code(self.cleaned_data["currency"])
        try:
            definition_for(currency)
        except UnknownCurrencyError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return currency

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if cleaned is None:
            return cleaned
        amount = cleaned.get("amount")
        currency = cleaned.get("currency")
        if amount is None or currency is None:
            return cleaned

        exponent = definition_for(currency).minor_unit_exponent
        quantum = Decimal(1).scaleb(-exponent)
        try:
            representable = amount.quantize(quantum)
        except InvalidOperation:
            self.add_error("amount", "Enter a valid amount for this currency.")
            return cleaned

        if amount != representable:
            decimal_label = "decimal places" if exponent != 1 else "decimal place"
            self.add_error(
                "amount",
                f"{currency} supports at most {exponent} {decimal_label}.",
            )
            return cleaned
        if representable <= 0:
            self.add_error("amount", "Amount must be greater than zero.")
            return cleaned

        cleaned["amount_minor"] = int(representable.scaleb(exponent))
        return cleaned

    @staticmethod
    def _initial_amount(instance: CanonicalTransaction) -> Decimal | None:
        """The amount to show in the edit form, if it can be read without a key.

        An encrypted amount is left blank rather than shown as ciphertext. The
        user re-enters it, which is the honest outcome: this form has no key.
        """

        if is_encrypted_value(instance.amount_encrypted or ""):
            return None
        try:
            return Decimal(instance.amount_encrypted.split(":", 1)[0]) / Decimal("100")
        except (InvalidOperation, ValueError):
            return None

    def save(
        self,
        *,
        data_key: bytes | None = None,
        blind_index_key: SearchKey | bytes | None = None,
        key_version: int = 1,
    ) -> CanonicalTransaction:
        """Persist the entry, encrypting its values when a key is supplied.

        The view always supplies one. The parameter is optional so a test can
        exercise the form without a key store behind it.
        """

        if not self.is_valid():
            raise ValueError("Call is_valid() before save().")
        data = self.cleaned_data
        kwargs = {
            "user": self.user,
            "occurred_at": data["occurred_at"],
            "amount_minor": data["amount_minor"],
            "currency": data["currency"],
            "transaction_type": data["transaction_type"],
            "financial_account": data["financial_account"],
            "payment_instrument": data.get("payment_instrument"),
            "category": data.get("category"),
            "merchant": data.get("merchant", ""),
            "counterparty": data.get("counterparty", ""),
            "notes": data.get("notes", ""),
        }
        if self.instance is None:
            return create_manual_transaction(
                **kwargs,
                data_key=data_key,
                blind_index_key=blind_index_key,
                key_version=key_version,
            )
        return update_manual_transaction(
            self.instance.pk,
            **kwargs,
            data_key=data_key,
            blind_index_key=blind_index_key,
            key_version=key_version,
        )
