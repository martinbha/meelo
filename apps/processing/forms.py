from __future__ import annotations

from typing import Any

from django import forms

from .storage import ALLOWED_UPLOAD_TYPES


class ScreenshotUploadForm(forms.Form):
    screenshot = forms.FileField(label="Screenshot", allow_empty_file=False)

    def clean_screenshot(self) -> Any:
        uploaded = self.cleaned_data["screenshot"]
        content_type = uploaded.content_type or ""
        if content_type not in ALLOWED_UPLOAD_TYPES:
            raise forms.ValidationError("Upload a PNG, JPEG, or WebP screenshot.")
        return uploaded
