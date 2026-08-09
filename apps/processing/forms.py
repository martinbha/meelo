from __future__ import annotations

from typing import Any

from django import forms

from .models import SourceDocument
from .validation import validate_uploaded_file


class ScreenshotUploadForm(forms.Form):
    screenshot = forms.FileField(label="Screenshot", allow_empty_file=False)
    retention_policy = forms.ChoiceField(
        choices=SourceDocument.RetentionPolicy.choices,
        initial=SourceDocument.RetentionPolicy.IMMEDIATE,
    )

    def clean_screenshot(self) -> Any:
        uploaded = self.cleaned_data["screenshot"]
        try:
            validate_uploaded_file(uploaded)
        except Exception as exc:
            raise forms.ValidationError(str(exc)) from exc
        return uploaded
