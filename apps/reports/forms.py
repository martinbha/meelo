"""The export request form.

The passphrase is required only for the encrypted format, and never remembered:
it is stretched into a key, used once, and dropped. Nothing stores it, which also
means nothing can recover an archive whose passphrase is forgotten — said plainly
on the page rather than discovered later.
"""

from __future__ import annotations

from typing import Any

from django import forms

from .exports import MINIMUM_PASSPHRASE_LENGTH
from .models import TransactionExport


class ExportRequestForm(forms.Form):
    """A format, an optional period, and a passphrase when one is needed."""

    export_format = forms.ChoiceField(choices=TransactionExport.Format.choices, label="Format")
    start = forms.DateField(required=False, label="From")
    end = forms.DateField(required=False, label="To")
    passphrase = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        min_length=MINIMUM_PASSPHRASE_LENGTH,
        label="Passphrase",
        help_text=(
            "Required for an encrypted archive. It is never stored, so an archive "
            "whose passphrase is lost cannot be opened by anyone, including you."
        ),
    )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        start = cleaned.get("start")
        end = cleaned.get("end")
        if start and end and end < start:
            raise forms.ValidationError("The end date cannot be before the start date.")
        if cleaned.get("export_format") == TransactionExport.Format.ENCRYPTED and not cleaned.get(
            "passphrase"
        ):
            self.add_error("passphrase", "An encrypted archive needs a passphrase.")
        return cleaned
