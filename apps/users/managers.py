from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import BaseUserManager

if TYPE_CHECKING:
    from .models import User


class UserManager(BaseUserManager[Any]):
    def create_user(self, email: str, password: str | None = None, **extra_fields: Any) -> "User":
        if not email:
            raise ValueError("An email address is required")
        normalized_email = self.normalize_email(email)
        user = self.model(email=normalized_email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> "User":
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if not extra_fields["is_staff"]:
            raise ValueError("A superuser must have is_staff=True")
        if not extra_fields["is_superuser"]:
            raise ValueError("A superuser must have is_superuser=True")
        return self.create_user(email, password, **extra_fields)
