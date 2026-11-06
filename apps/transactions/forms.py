from __future__ import annotations

from copy import copy
from decimal import Decimal
from typing import Any

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS

from apps.categorization.models import Category
from apps.core.blind_index import SearchKey
from apps.core.crypto import InvalidCiphertextError, is_encrypted_value
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument

from .models import CanonicalTransaction
from .money import read_money
from .services import create_manual_transaction, update_manual_transaction


class ManualTransactionForm(forms.Form):
    UNREADABLE_SOURCE_MESSAGE = (
        "One or more saved values could not be read. This transaction was not changed. "
        "Follow the transaction recovery instructions before editing it."
    )

    occurred_at = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=18, decimal_places=2)
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
        self,
        *,
        user: Any,
        instance: CanonicalTransaction | None = None,
        data_key: bytes | None = None,
        **kwargs: Any,
    ) -> None:
        self.user = user
        self.instance = instance
        self.data_key = data_key
        self._source_values_unreadable = False
        super().__init__(**kwargs)
        self.fields["financial_account"].queryset = FinancialAccount.objects.filter(  # type: ignore[attr-defined]
            user=user, is_active=True
        )
        self.fields["payment_instrument"].queryset = PaymentInstrument.objects.filter(  # type: ignore[attr-defined]
            user=user, is_active=True
        )
        self.fields["category"].queryset = Category.objects.filter(user=user)  # type: ignore[attr-defined]
        try:
            initial = self._read_initial_values(instance) if instance is not None else None
        except ValueError:
            # Never put an envelope or an unreadable value into a form field.
            # The non-field error also blocks a bound POST from saving blanks
            # over the original data.
            self._source_values_unreadable = True
        else:
            if instance is not None and not self.is_bound and initial is not None:
                self.initial.update(initial)

    def _read_initial_values(self, instance: CanonicalTransaction) -> dict[str, Any]:
        amount = read_money(instance, "amount_encrypted", data_key=self.data_key)
        merchant = self._read_editable_text(instance, "merchant_encrypted")
        counterparty = self._read_editable_text(instance, "counterparty_encrypted")
        notes = self._read_editable_text(instance, "notes_encrypted")
        return {
            "occurred_at": instance.occurred_at,
            "amount": amount.decimal_amount,
            "currency": str(amount.resolved_currency),
            "transaction_type": instance.transaction_type,
            "financial_account": instance.financial_account_id,
            "payment_instrument": instance.payment_instrument_id,
            "category": instance.category_id,
            "merchant": merchant,
            "counterparty": counterparty,
            "notes": notes,
        }

    def _read_editable_text(self, instance: CanonicalTransaction, field: str) -> str:
        value = instance.read_field(field, key=self.data_key)
        if is_encrypted_value(value):
            # A prefix alone is not proof: ordinary user text can start with
            # "v1.". Check whether the string authenticates as another value
            # for this exact field/record. A genuine historical nested
            # envelope is blocked, never displayed or automatically rewritten.
            probe = copy(instance)
            setattr(probe, field, value)
            try:
                probe.read_field(field, key=self.data_key)
            except InvalidCiphertextError:
                return value
            raise ValueError("A saved transaction value needs explicit recovery.")
        return value

    def full_clean(self) -> None:
        super().full_clean()
        if self._source_values_unreadable:
            errors = self.errors.setdefault(NON_FIELD_ERRORS, self.error_class())
            if self.UNREADABLE_SOURCE_MESSAGE not in errors:
                errors.append(self.UNREADABLE_SOURCE_MESSAGE)

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
            "amount_minor": int(data["amount"] * 100),
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
