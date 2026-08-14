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
