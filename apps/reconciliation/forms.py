"""Forms for linking two rows by hand.

The matcher misses relationships a person can see at a glance — a refund whose
merchant OCR'd badly, a transfer the bank dated a week late. This form is how
that judgement gets recorded, and its querysets are scoped to the requesting
user so a crafted post cannot reach across accounts.
"""

from __future__ import annotations

from typing import Any

from django import forms

from apps.core.ownership import owned_queryset
from apps.observations.models import ImportedObservation

from .models import ReconciliationMatch


class ManualLinkForm(forms.Form):
    """Two observations the user says belong together, and how."""

    left_observation = forms.ModelChoiceField(
        queryset=ImportedObservation.objects.none(), label="First row"
    )
    right_observation = forms.ModelChoiceField(
        queryset=ImportedObservation.objects.none(), label="Second row"
    )
    match_type = forms.ChoiceField(
        choices=ReconciliationMatch.MatchType.choices, label="Relationship"
    )

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Rejected and merged rows are excluded: a row already folded into
        # another cannot take part in a second relationship, and one the user
        # discarded is not a candidate for anything.
        linkable = (
            owned_queryset(ImportedObservation, user)
            .exclude(review_status__in=ImportedObservation.RESOLVED_STATUSES)
            .order_by("-occurred_at", "row_index", "pk")
        )
        self.fields["left_observation"].queryset = linkable  # type: ignore[attr-defined]
        self.fields["right_observation"].queryset = linkable  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        left = cleaned.get("left_observation")
        right = cleaned.get("right_observation")
        if left is not None and right is not None and left.pk == right.pk:
            raise forms.ValidationError("A link needs two different rows.")
        return cleaned
