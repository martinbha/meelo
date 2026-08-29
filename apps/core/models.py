from __future__ import annotations

import hashlib
import json
import uuid

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """Privacy-safe record of a security or financial workflow event."""

    class EventType(models.TextChoices):
        LOGIN_SUCCESS = "login_success", "Login succeeded"
        LOGIN_FAILURE = "login_failure", "Login failed"
        LOGOUT = "logout", "Logout"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        TWO_FACTOR_ENABLED = "two_factor_enabled", "Two-factor enabled"
        TWO_FACTOR_DISABLED = "two_factor_disabled", "Two-factor disabled"
        SCREENSHOT_UPLOADED = "screenshot_uploaded", "Screenshot uploaded"
        SCREENSHOT_DELETED = "screenshot_deleted", "Screenshot deleted"
        OCR_STARTED = "ocr_started", "OCR started"
        OCR_FAILED = "ocr_failed", "OCR failed"
        TRANSACTION_CREATED = "transaction_created", "Transaction created"
        TRANSACTION_ACCEPTED = "transaction_accepted", "Transaction accepted"
        TRANSACTION_CORRECTED = "transaction_corrected", "Transaction corrected"
        TRANSACTION_VOIDED = "transaction_voided", "Transaction voided"
        OPENING_BALANCE_POSTED = "opening_balance_posted", "Opening balance posted"
        OPENING_BALANCE_ADJUSTED = "opening_balance_adjusted", "Opening balance adjusted"
        TRANSACTION_DELETED = "transaction_deleted", "Transaction deleted"
        OBSERVATIONS_IMPORTED = "observations_imported", "Observations imported"
        OBSERVATION_CORRECTED = "observation_corrected", "Observation corrected"
        OBSERVATION_ACCEPTED = "observation_accepted", "Observation accepted"
        OBSERVATION_REJECTED = "observation_rejected", "Observation rejected"
        OBSERVATION_MERGED = "observation_merged", "Observation merged"
        DOCUMENT_REPROCESS_REQUESTED = "document_reprocess_requested", "Reprocessing requested"
        DOCUMENT_OVERRIDE_SET = "document_override_set", "Document override set"
        DOCUMENT_OVERRIDE_CLEARED = "document_override_cleared", "Document override cleared"
        RECONCILIATION_MATCH_CREATED = "reconciliation_match_created", "Match proposed"
        RECONCILIATION_MATCH_CONFIRMED = "reconciliation_match_confirmed", "Match confirmed"
        RECONCILIATION_MATCH_REJECTED = "reconciliation_match_rejected", "Match rejected"
        RECONCILIATION_MATCH_UNLINKED = "reconciliation_match_unlinked", "Match unlinked"
        DUPLICATE_MERGED = "duplicate_merged", "Duplicate merged"
        INTERNAL_TRANSFER_CONFIRMED = "internal_transfer_confirmed", "Internal transfer confirmed"
        REFUND_MATCHED = "refund_matched", "Refund matched to a purchase"
        CATEGORY_CHANGED = "category_changed", "Category changed"
        PAYMENT_INSTRUMENT_CHANGED = "payment_instrument_changed", "Payment instrument changed"
        MERCHANT_ALIAS_CREATED = "merchant_alias_created", "Merchant alias created"
        CATEGORY_RULE_CREATED = "category_rule_created", "Category rule created"
        CATEGORY_RULE_ENABLED = "category_rule_enabled", "Category rule enabled"
        CATEGORY_RULE_DISABLED = "category_rule_disabled", "Category rule disabled"
        CATEGORY_RULE_APPLIED = "category_rule_applied", "Category rule applied"
        EXPORT_CREATED = "export_created", "Export created"
        EXPORT_DOWNLOADED = "export_downloaded", "Export downloaded"
        ENCRYPTION_KEY_ROTATED = "encryption_key_rotated", "Encryption key rotated"
        ENCRYPTION_KEY_PROVISIONED = "encryption_key_provisioned", "Encryption key provisioned"
        ENCRYPTION_KEY_ACCESSED = "encryption_key_accessed", "Encryption key accessed"
        WORKER_KEY_ACCESSED = "worker_key_accessed", "Worker opened the owner's key"
        SEARCH_KEY_PROVISIONED = "search_key_provisioned", "Search key provisioned"
        SEARCH_KEY_ROTATED = "search_key_rotated", "Search key rotated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )
    event_type = models.CharField(max_length=64, choices=EventType.choices)
    object_type = models.CharField(max_length=128, blank=True)
    object_id = models.UUIDField(blank=True, null=True)
    request_id = models.CharField(max_length=64, blank=True)
    ip_hash = models.CharField(max_length=128, blank=True)
    user_agent_hash = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict)
    previous_digest = models.CharField(max_length=128, blank=True)
    digest = models.CharField(max_length=128, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("user", "created_at"), name="audit_user_created_idx"),
            models.Index(fields=("user", "event_type"), name="audit_user_event_idx"),
        ]

    def calculate_digest(self) -> str:
        payload = {
            "user_id": str(self.user_id),
            "event_type": self.event_type,
            "object_type": self.object_type,
            "object_id": str(self.object_id) if self.object_id else "",
            "request_id": self.request_id,
            "ip_hash": self.ip_hash,
            "user_agent_hash": self.user_agent_hash,
            "metadata": self.metadata,
            "previous_digest": self.previous_digest,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def verify_digest(self) -> bool:
        return self.digest == self.calculate_digest()

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.digest:
            self.digest = self.calculate_digest()
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class WorkerHeartbeat(models.Model):
    """Last-seen state for database-backed workers.

    A heartbeat is deliberately an operational row rather than a network
    endpoint. The worker has no listener to expose, and the web process can
    still report a stale worker from the same database it already trusts.
    """

    worker_id = models.CharField(max_length=128, primary_key=True)
    last_seen_at = models.DateTimeField()
    last_job_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("-last_seen_at",), name="worker_heartbeat_seen_idx")]

    @classmethod
    def touch(cls, worker_id: str, *, job_seen: bool = False) -> None:
        """Record one poll without storing task payloads or user data."""

        from django.utils import timezone

        now = timezone.now()
        defaults = {"last_seen_at": now}
        if job_seen:
            defaults["last_job_at"] = now
        cls.objects.update_or_create(worker_id=worker_id, defaults=defaults)


class RotationCheckpoint(models.Model):
    """How far a key rotation got, per user, per model.

    The envelope version already makes rotation *correct* to re-run: a row
    sealed under the target version is skipped. What it does not make it is
    cheap. Without a checkpoint every resumed run re-reads the whole history
    from the first row to find the point it stopped at, and on the rotation
    that matters — the one over years of transactions, resumed after a crash —
    that is the difference between minutes and hours.

    So this records where the walk got to and the next run starts after it. It
    is an optimisation, deliberately: if the checkpoint is wrong, stale, or
    missing, the worst outcome is work done twice, never a row left behind. The
    skip-by-version check remains the thing that guarantees correctness.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rotation_checkpoints",
    )
    #: The version being rotated *to*. A checkpoint from an earlier rotation
    #: says nothing about this one, so the version is part of the identity
    #: rather than a column that gets overwritten.
    key_version = models.PositiveIntegerField()
    #: Which key this rotation is moving: the data key that encrypts values, or
    #: the search key that indexes them. They rotate separately (#161), so a
    #: checkpoint for one says nothing about the other and the two must not
    #: share a row.
    key_kind = models.CharField(max_length=16, default="data")
    model_label = models.CharField(max_length=128)
    #: The primary key of the last row this rotation finished. Text, because
    #: the models it tracks use UUIDs and one column has to hold all of them.
    last_record_id = models.CharField(max_length=64, blank=True)
    rows_rotated = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user", "key_kind", "key_version", "model_label")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "key_kind", "key_version", "model_label"),
                name="rotation_checkpoint_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "key_version"), name="rotation_checkpoint_idx"),
        ]

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    def __str__(self) -> str:
        return f"{self.key_kind}:{self.model_label} -> v{self.key_version}"
