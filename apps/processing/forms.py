from __future__ import annotations

from typing import Any

from django import forms

from .models import SourceDocument
from .overrides import institution_choices
from .validation import validate_uploaded_file


class ScreenshotUploadForm(forms.Form):
    screenshot = forms.FileField(label="Screenshot", allow_empty_file=False)
    retention_policy = forms.ChoiceField(
        choices=SourceDocument.RetentionPolicy.choices,
        initial=SourceDocument.RetentionPolicy.IMMEDIATE,
    )

    def clean_screenshot(self) -> Any:
        if len(self.files.getlist("screenshot")) != 1:
            raise forms.ValidationError("Upload exactly one screenshot at a time.")
        uploaded = self.cleaned_data["screenshot"]
        try:
            validate_uploaded_file(uploaded)
        except Exception as exc:
            raise forms.ValidationError(str(exc)) from exc
        return uploaded


class DocumentOverrideForm(forms.Form):
    """What a reviewer says a screenshot is, when detection got it wrong.

    Both fields are optional and both are submitted together: the form posts
    the whole override, so leaving a field empty clears it. A form that could
    only add would leave a reviewer no way back to automatic detection.
    """

    source_type = forms.ChoiceField(
        required=False,
        label="Screenshot type",
        choices=[("", "Detect automatically"), *SourceDocument.SourceType.choices],
    )
    institution = forms.ChoiceField(
        required=False,
        label="Institution",
        # Read from the parser registry at construction time rather than at
        # import time, so a newly registered parser appears without a restart
        # and a removed one cannot be offered.
        choices=[],
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["institution"].choices = [  # type: ignore[attr-defined]
            ("", "Detect automatically"),
            *institution_choices(),
        ]
