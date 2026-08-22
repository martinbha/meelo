"""Export files, tracked so they can be found again and deleted on time.

An export is the one place a user's confirmed financial history leaves the
encrypted store as a readable file. That makes the row describing it as important
as the file itself: without it nothing knows the file exists, who it belongs to,
or when it should stop existing.

So the row carries an owner, an expiry, and the path — and never the contents.
Everything a database reader could learn from it is metadata: a format, a row
count, a size, a date range (specification 23, 25.5).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

#: How long an export stays downloadable. Short on purpose: a plaintext CSV of
#: somebody's finances sitting on disk is the largest unencrypted surface this
#: system ever creates, and it exists so a person can save it somewhere else.
DEFAULT_EXPORT_LIFETIME = timedelta(hours=1)


class TransactionExport(models.Model):
    """One generated export, its expiry, and where the file is."""

    class Format(models.TextChoices):
        CSV = "csv", "CSV"
        JSON = "json", "JSON"
        #: A JSON export sealed with a key derived from a passphrase the user
        #: chose. The only form safe to keep after the expiry passes.
        ENCRYPTED = "encrypted", "Encrypted archive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="transaction_exports",
    )
    export_format = models.CharField(max_length=16, choices=Format.choices)
    #: Absolute path inside the private export root. Never served directly; the
    #: download view streams it after checking ownership and expiry.
    file_path = models.TextField()
    file_size = models.PositiveBigIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    period_start = models.DateField(blank=True, null=True)
    period_end = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    downloaded_at = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "-created_at"), name="export_user_created_idx"),
            models.Index(fields=("expires_at",), name="export_expiry_idx"),
        ]
        # No constraint tying expires_at to created_at, deliberately.
        # Shortening an expiry to force a cleanup is a legitimate operation, and
        # a CHECK would forbid it to prevent a mistake nothing makes: the
        # lifetime is validated in ``create_export``, which is the only place a
        # nonsense expiry could be written.

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_available(self) -> bool:
        """Whether the file should still be handed to its owner."""

        return self.deleted_at is None and not self.is_expired

    @property
    def is_encrypted(self) -> bool:
        return self.export_format == self.Format.ENCRYPTED

    @property
    def filename(self) -> str:
        """A name for the download, carrying no financial detail."""

        suffix = {
            self.Format.CSV: "csv",
            self.Format.JSON: "json",
            self.Format.ENCRYPTED: "enc",
        }[self.Format(self.export_format)]
        return f"transactions-{self.created_at:%Y%m%d-%H%M%S}.{suffix}"

    def __str__(self) -> str:
        return f"{self.export_format} export ({self.row_count} rows)"


class QualityMetricDaily(models.Model):
    """Privacy-safe daily quality aggregates.

    The dimensions are parser and source-type names, not user data. Every other
    value is a count or a rate, so this table can be used for trend analysis
    without becoming a second financial ledger.
    """

    day = models.DateField()
    institution = models.CharField(max_length=64)
    source_type = models.CharField(max_length=40)
    observations_count = models.PositiveIntegerField(default=0)
    corrected_count = models.PositiveIntegerField(default=0)
    disagreement_count = models.PositiveIntegerField(default=0)
    duplicate_candidates_count = models.PositiveIntegerField(default=0)
    duplicate_confirmed_count = models.PositiveIntegerField(default=0)
    ocr_issue_count = models.PositiveIntegerField(default=0)
    parser_issue_count = models.PositiveIntegerField(default=0)
    correction_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    disagreement_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    duplicate_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    ocr_issue_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    parser_issue_rate = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("day", "institution", "source_type"),
                name="quality_metric_daily_dimension_unique",
            )
        ]
        indexes = [
            models.Index(fields=("day", "institution"), name="quality_metric_daily_day_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.day}: {self.institution}/{self.source_type}"
