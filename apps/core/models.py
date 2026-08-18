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
        SCREENSHOT_UPLOADED = "screenshot_uploaded", "Screenshot uploaded"
        SCREENSHOT_DELETED = "screenshot_deleted", "Screenshot deleted"
        OCR_STARTED = "ocr_started", "OCR started"
        OCR_FAILED = "ocr_failed", "OCR failed"
        TRANSACTION_CREATED = "transaction_created", "Transaction created"
        TRANSACTION_ACCEPTED = "transaction_accepted", "Transaction accepted"
        TRANSACTION_CORRECTED = "transaction_corrected", "Transaction corrected"
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
