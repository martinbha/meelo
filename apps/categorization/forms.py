"""Forms for turning a reviewer's correction into a reusable rule.

The scope field is required and has no default. A pre-selected scope is a
guess, and the guess this form exists to avoid is the one that reclassifies a
year of history from a single correction.
"""

from __future__ import annotations

from typing import Any

from django import forms

from apps.core.ownership import owned_queryset

from .models import Category
from .rule_creation import SCOPE_LABELS, RuleScope


class CategoryCorrectionForm(forms.Form):
    """A category, how widely it should apply, and whether to look backwards."""

    category = forms.ModelChoiceField(queryset=Category.objects.none(), label="Category")
    scope = forms.ChoiceField(
        choices=[(scope.value, SCOPE_LABELS[scope]) for scope in RuleScope],
        widget=forms.RadioSelect,
        label="Apply this to",
    )
    apply_to_existing = forms.BooleanField(
        required=False,
        initial=False,
        label="Also recategorise transactions I have not confirmed yet",
        help_text="Confirmed history is never changed.",
    )

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = owned_queryset(Category, user)  # type: ignore[attr-defined]
