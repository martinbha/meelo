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
        DUPLICATE_MERGED = "duplicate_merged", "Duplicate merged"
        CATEGORY_CHANGED = "category_changed", "Category changed"
        EXPORT_CREATED = "export_created", "Export created"
        ENCRYPTION_KEY_ROTATED = "encryption_key_rotated", "Encryption key rotated"

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
