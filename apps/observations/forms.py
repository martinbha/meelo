"""Forms for correcting a parsed observation.

Validation lives here so an invalid correction is refused before it can reach
the encrypted store, and so the reviewer sees which field was wrong rather than
a generic failure.
"""

from __future__ import annotations

from typing import Any

from django import forms

from apps.categorization.models import Category
from apps.core.ownership import owned_queryset
from apps.financial_accounts.models import FinancialAccount
from apps.instruments.models import PaymentInstrument
from apps.transactions.models import CanonicalTransaction

from .models import ImportedObservation


class ObservationCorrectionForm(forms.Form):
    """Field-level corrections for one observation."""

    occurred_at = forms.DateField(required=False)
    posted_at = forms.DateField(required=False)
    merchant = forms.CharField(required=False, max_length=255, strip=True)
    amount_minor = forms.IntegerField(required=False, min_value=1)
    currency = forms.CharField(required=False, min_length=3, max_length=3)
    direction = forms.ChoiceField(required=False, choices=ImportedObservation.Direction.choices)
    transaction_type_guess = forms.ChoiceField(
        required=False, choices=CanonicalTransaction.TransactionType.choices
    )
    installment_months = forms.IntegerField(required=False, min_value=1)
    financial_account_guess = forms.ModelChoiceField(
        required=False, queryset=FinancialAccount.objects.none()
    )
    payment_instrument_guess = forms.ModelChoiceField(
        required=False, queryset=PaymentInstrument.objects.none()
    )
    category_guess = forms.ModelChoiceField(required=False, queryset=Category.objects.none())

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Choices are scoped to the reviewer so a crafted form cannot attach
        # another user's account, card, or category to an observation.
        self.fields["financial_account_guess"].queryset = owned_queryset(  # type: ignore[attr-defined]
            FinancialAccount, user
        )
        self.fields["payment_instrument_guess"].queryset = owned_queryset(  # type: ignore[attr-defined]
            PaymentInstrument, user
        )
        self.fields["category_guess"].queryset = owned_queryset(Category, user)  # type: ignore[attr-defined]

    def clean_currency(self) -> str:
        value = (self.cleaned_data.get("currency") or "").strip().upper()
        if value and not value.isalpha():
            raise forms.ValidationError("Currency must be three letters.")
        return value

    def corrections(self) -> dict[str, Any]:
        """Only the fields the reviewer actually submitted.

        An untouched field must not be sent as a correction, or every save
        would mark the whole row as corrected.
        """

        submitted = {}
        for name, value in self.cleaned_data.items():
            if name not in self.changed_data:
                continue
            if value in ("", None) and name not in {"posted_at", "installment_months"}:
                continue
            submitted[name] = value
        return submitted


class ReviewActionForm(forms.Form):
    """Confirmation flag carried by the accept action."""

    confirmed = forms.BooleanField(required=False)
    transaction_type = forms.ChoiceField(
        required=False, choices=CanonicalTransaction.TransactionType.choices
    )
    financial_account = forms.ModelChoiceField(
        required=False, queryset=FinancialAccount.objects.none()
    )

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["financial_account"].queryset = owned_queryset(  # type: ignore[attr-defined]
            FinancialAccount, user
        )


class MergeForm(forms.Form):
    """Identifiers of the rows being folded into the one being reviewed."""

    duplicate_ids = forms.CharField(required=True)

    def clean_duplicate_ids(self) -> list[str]:
        raw = self.cleaned_data["duplicate_ids"]
        values = [item.strip() for item in raw.split(",") if item.strip()]
        if not values:
            raise forms.ValidationError("Select at least one duplicate to merge.")
        return values
