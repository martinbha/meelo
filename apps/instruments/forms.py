from __future__ import annotations

from typing import Any

from django import forms

from apps.financial_accounts.models import FinancialAccount

from .models import PaymentInstrument
from .services import update_instrument_mapping


class PaymentInstrumentMappingForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = PaymentInstrument
        fields = ("financial_account", "settlement_account", "is_active")

    def __init__(self, *, user: Any, **kwargs: Any) -> None:
        self.user = user
        super().__init__(**kwargs)
        accounts = FinancialAccount.objects.filter(user=user, is_active=True)
        self.fields["financial_account"].queryset = accounts  # type: ignore[attr-defined]
        self.fields["settlement_account"].queryset = accounts  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if self.instance.user_id != self.user.pk:
            raise forms.ValidationError("Payment instrument not found.")
        return cleaned

    def save(self, commit: bool = True) -> PaymentInstrument:
        if not commit:
            raise ValueError("Mapping changes must be saved through the audited service.")
        return update_instrument_mapping(
            self.instance.pk,
            user=self.user,
            financial_account=self.cleaned_data["financial_account"],
            settlement_account=self.cleaned_data.get("settlement_account"),
            is_active=self.cleaned_data["is_active"],
        )
