from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.encrypted_fields import EncryptedFieldsMixin

from .retry import is_retryable_error, retry_delay


class SourceDocument(EncryptedFieldsMixin, models.Model):
    """Metadata and lifecycle state for one user-uploaded screenshot."""

    encrypted_fields = (
        "original_filename_encrypted",
        "source_institution_guess_encrypted",
        "error_message_encrypted",
    )

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

    class RetentionPolicy(models.TextChoices):
        IMMEDIATE = "immediate", "Delete after processing"
        ONE_DAY = "one_day", "Retain for one day"
        SEVEN_DAYS = "seven_days", "Retain for seven days"
        THIRTY_DAYS = "thirty_days", "Retain for thirty days"

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
    #: What a reviewer said this screenshot actually is, when detection got it
    #: wrong. Kept apart from ``source_type`` rather than overwriting it: the
    #: detected guess is evidence about how well detection works, and a reviewer
    #: who changes their mind has to be able to get back to automatic behaviour.
    #: Blank means "trust detection", which is why it is blank rather than null —
    #: two ways to say "no override" is one more than the parser should have to
    #: check.
    source_type_override = models.CharField(max_length=40, choices=SourceType.choices, blank=True)
    #: The institution parser a reviewer chose, by name. Validated against the
    #: registered parsers when it is set, so a stale name cannot silently fall
    #: back to detection on the next pass — that would look like the override
    #: was honoured when it was not.
    institution_override = models.CharField(max_length=64, blank=True)
    processing_status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING
    )
    error_code = models.CharField(max_length=64, blank=True)
    error_message_encrypted = models.TextField(blank=True)
    cleanup_error_code = models.CharField(max_length=64, blank=True)
    temporary_path = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    processing_started_at = models.DateTimeField(blank=True, null=True)
    processing_completed_at = models.DateTimeField(blank=True, null=True)
    processing_attempt_count = models.PositiveIntegerField(default=0)
    next_processing_attempt_at = models.DateTimeField(blank=True, null=True)
    original_deleted_at = models.DateTimeField(blank=True, null=True)
    #: Difference hash of the original image, used to spot screenshots that
    #: differ only by crop or recompression. Optional: exact SHA-256 duplicate
    #: detection works without it.
    perceptual_hash = models.CharField(max_length=32, blank=True, db_index=True)
    retention_policy = models.CharField(
        max_length=16, choices=RetentionPolicy.choices, default=RetentionPolicy.IMMEDIATE
    )
    retention_deadline = models.DateTimeField(blank=True, null=True)

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

    @property
    def effective_source_type(self) -> str:
        """What the parsers should treat this document as.

        The reviewer's correction wins over detection. Everything downstream
        reads this rather than ``source_type``, so there is one answer to the
        question instead of one per caller.
        """

        return self.source_type_override or self.source_type

    @property
    def has_overrides(self) -> bool:
        return bool(self.source_type_override or self.institution_override)

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

        from apps.core import metrics

        now = timezone.now()
        with metrics.timed(metrics.DATABASE_QUEUE_CLAIM), transaction.atomic():
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
    ) -> bool:
        """Record an error and requeue only classified transient failures.

        Returns ``True`` when the job was requeued and ``False`` when it is
        terminally failed.
        """

        now = timezone.now()
        self.last_error_code = code[:64]
        self.last_error_message = message
        self.locked_at = None
        retryable = is_retryable_error(code)
        if retryable and self.attempt_count < self.max_attempts:
            self.status = self.Status.QUEUED
            self.available_at = now + retry_delay(self.attempt_count)
            self.completed_at = None
            with transaction.atomic():
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
                SourceDocument.objects.filter(
                    pk=self.document_id,
                    user=self.user,
                    processing_status=SourceDocument.Status.FAILED,
                ).update(
                    processing_status=SourceDocument.Status.QUEUED,
                    processing_completed_at=None,
                    next_processing_attempt_at=self.available_at,
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
        with transaction.atomic():
            stale = list(
                cls.objects.select_for_update(skip_locked=True).filter(
                    status=cls.Status.RUNNING, locked_at__lt=cutoff
                )
            )
            if not stale:
                return 0
            job_ids = [job.pk for job in stale]
            document_ids = [job.document_id for job in stale]
            recovered = cls.objects.filter(pk__in=job_ids).update(
                status=cls.Status.QUEUED,
                available_at=now,
                locked_at=None,
                started_at=None,
                updated_at=now,
            )
            SourceDocument.objects.filter(
                id__in=document_ids,
                processing_status__in=[
                    SourceDocument.Status.VALIDATING,
                    SourceDocument.Status.PREPROCESSING,
                    SourceDocument.Status.OCR_RUNNING,
                    SourceDocument.Status.PARSING,
                ],
            ).update(
                processing_status=SourceDocument.Status.QUEUED,
                next_processing_attempt_at=now,
            )
        return recovered
