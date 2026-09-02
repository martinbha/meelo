import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    encryption_key_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email


class UserDataKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="data_keys")
    version = models.PositiveIntegerField()
    wrapped_key = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("user", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "version"), name="user_data_key_version_unique"
            ),
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(is_active=True),
                name="user_data_key_one_active",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="user_data_key_version_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"user-data-key-v{self.version}"


class UserSearchKey(models.Model):
    """The HMAC key behind one user's blind indexes.

    A separate row rather than a column on :class:`UserDataKey`, because the two
    keys rotate on different schedules and for different reasons. Rotating the
    encryption key means re-encrypting every value; rotating the search key
    means recomputing every index. Sharing a row would force one whenever the
    other was wanted, and a system where rotation is expensive is a system where
    rotation does not happen.

    The key is derived from the *master* key with its own label, not from the
    data key. That is the whole point of the separation: a leaked data key
    should not also hand over the ability to build search tokens and confirm
    guesses against the index (specification 22.4).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="search_keys")
    version = models.PositiveIntegerField()
    wrapped_key = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("user", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "version"), name="user_search_key_version_unique"
            ),
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(is_active=True),
                name="user_search_key_one_active",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="user_search_key_version_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"user-search-key-v{self.version}"


class RecoveryCode(models.Model):
    """A one-time two-factor fallback stored only as a password hash."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=256)
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("user", "used_at"), name="recovery_user_unused_idx")]


class UserSession(models.Model):
    """Privacy-safe metadata for one Django session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tracked_sessions")
    session_key_hash = models.CharField(max_length=64, unique=True)
    ip_hash = models.CharField(max_length=64)
    user_agent_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [models.Index(fields=("user", "revoked_at"), name="session_user_active_idx")]
