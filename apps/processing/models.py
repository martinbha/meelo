from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Self

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class SourceDocument(models.Model):
    """Metadata and lifecycle state for one user-uploaded screenshot."""

    class SourceType(models.TextChoices):
        BANK_TRANSACTION_LIST = "bank_transaction_list", "Bank transaction list"
        BANK_TRANSACTION_DETAIL = "bank_transaction_detail", "Bank transaction detail"
        BANK_TRANSFER_CONFIRMATION = "bank_transfer_confirmation", "Bank transfer confirmation"
        CARD_TRANSACTION_LIST = "card_transaction_list", "Card transaction list"
        CARD_TRANSACTION_DETAIL = "card_transaction_detail", "Card transaction detail"
        CREDIT_CARD_STATEMENT = "credit_card_statement", "Credit-card statement"
        CREDIT_CARD_PAYMENT = "credit_card_payment", "Credit-card payment"
        UNKNOWN = "unknown", "Unknown"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        VALIDATING = "validating", "Validating"
        QUEUED = "queued", "Queued"
        PREPROCESSING = "preprocessing", "Preprocessing"
        OCR_RUNNING = "ocr_running", "OCR running"
        PARSING = "parsing", "Parsing"
        READY_FOR_REVIEW = "ready_for_review", "Ready for review"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="source_documents",
    )
    file_sha256 = models.CharField(max_length=64, db_index=True)
    original_filename_encrypted = models.TextField()
    mime_type = models.CharField(max_length=100)
    file_size = models.PositiveBigIntegerField()
    image_width = models.PositiveIntegerField(blank=True, null=True)
    image_height = models.PositiveIntegerField(blank=True, null=True)
    source_institution_guess_encrypted = models.TextField(blank=True)
    source_type = models.CharField(
        max_length=40, choices=SourceType.choices, default=SourceType.UNKNOWN
    )
    processing_status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING
    )
    error_code = models.CharField(max_length=64, blank=True)
    error_message_encrypted = models.TextField(blank=True)
    temporary_path = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    processing_started_at = models.DateTimeField(blank=True, null=True)
    processing_completed_at = models.DateTimeField(blank=True, null=True)
    processing_attempt_count = models.PositiveIntegerField(default=0)
    next_processing_attempt_at = models.DateTimeField(blank=True, null=True)
    original_deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-uploaded_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "file_sha256"), name="source_document_user_sha256_unique"
            ),
        ]
        indexes = [
            models.Index(fields=("user", "processing_status"), name="source_doc_user_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename_encrypted} ({self.processing_status})"


class ProcessingJob(models.Model):
    """A durable, database-backed unit of asynchronous document processing."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="processing_jobs",
    )
    # SourceDocument is introduced by a later issue, so this queue stores its
    # identifier without coupling the migration graph to that future app.
    document_id = models.UUIDField(db_index=True)
    task_name = models.CharField(max_length=100)
    # Payloads must contain JSON primitives and identifiers, never ORM objects.
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(
                fields=("status", "available_at", "created_at"),
                name="processing_job_queue_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(max_attempts__gte=1),
                name="processing_job_max_attempts_positive",
            ),
        ]

    @classmethod
    def for_user(cls, user: object) -> Any:
        """Return jobs through the owner boundary used by web and worker code."""

        user_id = getattr(user, "pk", None)
        if user_id is None or not getattr(user, "is_authenticated", False):
            return cls.objects.none()
        return cls.objects.filter(user_id=user_id)

    @classmethod
    def claim_next(cls) -> Self | None:
        """Atomically claim the oldest available queued job."""

        now = timezone.now()
        with transaction.atomic():
            job = (
                cls.objects.select_for_update(skip_locked=True)
                .filter(status=cls.Status.QUEUED, available_at__lte=now)
                .order_by("available_at", "created_at")
                .first()
            )
            if job is None:
                return None

            job.status = cls.Status.RUNNING
            job.attempt_count += 1
            job.locked_at = now
            job.started_at = now
            job.completed_at = None
            job.last_error_code = ""
            job.last_error_message = ""
            job.save(
                update_fields=[
                    "status",
                    "attempt_count",
                    "locked_at",
                    "started_at",
                    "completed_at",
                    "last_error_code",
                    "last_error_message",
                    "updated_at",
                ]
            )
            return job

    def mark_succeeded(self) -> None:
        now = timezone.now()
        self.status = self.Status.SUCCEEDED
        self.locked_at = None
        self.completed_at = now
        self.save(update_fields=["status", "locked_at", "completed_at", "updated_at"])

    def mark_failed(
        self,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> bool:
        """Record an error and optionally requeue while attempts remain.

        Returns ``True`` when the job was requeued and ``False`` when it is
        terminally failed.
        """

        now = timezone.now()
        self.last_error_code = code[:64]
        self.last_error_message = message
        self.locked_at = None
        if retryable and self.attempt_count < self.max_attempts:
            delay = min(300, 2 ** max(self.attempt_count - 1, 0))
            self.status = self.Status.QUEUED
            self.available_at = now + timedelta(seconds=delay)
            self.completed_at = None
            self.save(
                update_fields=[
                    "status",
                    "available_at",
                    "locked_at",
                    "completed_at",
                    "last_error_code",
                    "last_error_message",
                    "updated_at",
                ]
            )
            return True

        self.status = self.Status.FAILED
        self.completed_at = now
        self.save(
            update_fields=[
                "status",
                "locked_at",
                "completed_at",
                "last_error_code",
                "last_error_message",
                "updated_at",
            ]
        )
        return False

    @classmethod
    def recover_stale(cls, *, cutoff: datetime) -> int:
        """Return abandoned running jobs to the queue."""

        now = timezone.now()
        return cls.objects.filter(status=cls.Status.RUNNING, locked_at__lt=cutoff).update(
            status=cls.Status.QUEUED,
            available_at=now,
            locked_at=None,
            started_at=None,
            updated_at=now,
        )
