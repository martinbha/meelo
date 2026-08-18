from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.encrypted_fields import EncryptedFieldsMixin
from apps.processing.models import SourceDocument


class OcrRun(EncryptedFieldsMixin, models.Model):
    encrypted_fields = (
        "configuration_encrypted",
        "preprocessing_encrypted",
        "raw_output_encrypted",
    )

    class Engine(models.TextChoices):
        """The engines specification 6.5 allows a run to come from.

        Named here rather than left as free text so a typo cannot create a
        third engine that consensus silently counts as an independent opinion.
        """

        PADDLEOCR = "paddleocr", "PaddleOCR"
        TESSERACT = "tesseract", "Tesseract"

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
    engine = models.CharField(max_length=32, choices=Engine.choices)
    engine_version = models.CharField(max_length=128)
    model_versions = models.JSONField(default=dict, blank=True)
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


class OcrToken(EncryptedFieldsMixin, models.Model):
    encrypted_fields = ("text_encrypted", "normalized_text_encrypted")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ocr_tokens",
    )
    ocr_run = models.ForeignKey(OcrRun, on_delete=models.CASCADE, related_name="tokens")
    text_encrypted = models.TextField()
    normalized_text_encrypted = models.TextField()
    confidence = models.FloatField()
    left = models.PositiveIntegerField()
    top = models.PositiveIntegerField()
    right = models.PositiveIntegerField()
    bottom = models.PositiveIntegerField()
    page_number = models.PositiveIntegerField(default=0)
    block_number = models.PositiveIntegerField(default=0)
    paragraph_number = models.PositiveIntegerField(default=0)
    line_number = models.PositiveIntegerField(default=0)
    word_number = models.PositiveIntegerField(default=0)
    sequence = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("sequence",)
        constraints = [
            models.UniqueConstraint(
                fields=("ocr_run", "sequence"), name="ocr_token_run_sequence_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0, confidence__lte=1.0),
                name="ocr_token_confidence_range",
            ),
            models.CheckConstraint(
                condition=models.Q(right__gte=models.F("left")),
                name="ocr_token_horizontal_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(bottom__gte=models.F("top")),
                name="ocr_token_vertical_bounds",
            ),
        ]
        indexes = [
            models.Index(fields=("ocr_run", "sequence"), name="ocr_token_reading_idx"),
            models.Index(fields=("ocr_run", "top", "left"), name="ocr_token_spatial_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.ocr_run_id and self.user_id:
            owner_id = (
                OcrRun.objects.filter(pk=self.ocr_run_id).values_list("user_id", flat=True).first()
            )
            if owner_id != self.user_id:
                raise ValidationError(
                    {"ocr_run": "The OCR token run must belong to the same user."}
                )

    def __str__(self) -> str:
        return f"token {self.sequence} for {self.ocr_run_id}"
