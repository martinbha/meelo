from __future__ import annotations

from typing import Any

from django import forms

from .models import FinancialAccount


class FinancialAccountSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Edit non-sensitive account settings without exposing encrypted display fields."""

    class Meta:
        model = FinancialAccount
        fields = ("account_type", "currency", "identifier_last_four", "is_active")

    def __init__(self, *, user: Any, **kwargs: Any) -> None:
        self.user = user
        super().__init__(**kwargs)

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if self.instance.user_id != self.user.pk:
            raise forms.ValidationError("Financial account not found.")
        return cleaned
