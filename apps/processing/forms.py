from __future__ import annotations

from typing import Any

from django import forms

from .validation import validate_uploaded_file


class ScreenshotUploadForm(forms.Form):
    screenshot = forms.FileField(label="Screenshot", allow_empty_file=False)

    def clean_screenshot(self) -> Any:
        uploaded = self.cleaned_data["screenshot"]
        try:
            validate_uploaded_file(uploaded)
        except Exception as exc:
            raise forms.ValidationError(str(exc)) from exc
        return uploaded
