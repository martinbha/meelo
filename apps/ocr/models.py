from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.processing.models import SourceDocument


class OcrRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ocr_runs",
    )
    source_document = models.ForeignKey(
        SourceDocument,
        on_delete=models.CASCADE,
        related_name="ocr_runs",
    )
    engine = models.CharField(max_length=32)
    engine_version = models.CharField(max_length=128)
    model_versions = models.JSONField(default=dict)
    languages = models.JSONField(default=list)
    configuration_encrypted = models.TextField()
    preprocessing_encrypted = models.TextField(blank=True)
    selected_preprocessing_variant = models.CharField(max_length=32, blank=True)
    raw_output_encrypted = models.TextField(blank=True)
    succeeded = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(
                fields=("user", "source_document", "created_at"),
                name="ocr_run_document_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.source_document_id and self.user_id:
            owner_id = (
                SourceDocument.objects.filter(pk=self.source_document_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if owner_id != self.user_id:
                raise ValidationError(
                    {"source_document": "The OCR run document must belong to the same user."}
                )

    def __str__(self) -> str:
        return f"{self.engine} run for {self.source_document_id}"
