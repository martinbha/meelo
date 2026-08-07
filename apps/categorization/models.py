from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Category(models.Model):
    class CategoryType(models.TextChoices):
        EXPENSE = "expense", "Expense"
        INCOME = "income", "Income"
        TRANSFER = "transfer", "Transfer"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name_encrypted = models.TextField()
    name_blind_index = models.CharField(max_length=128)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        blank=True,
        null=True,
    )
    category_type = models.CharField(max_length=16, choices=CategoryType.choices)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name_blind_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "parent", "name_blind_index"),
                name="category_user_parent_name_blind_unique",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "parent"), name="category_user_parent_idx"),
            models.Index(fields=("user", "category_type"), name="category_user_type_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.parent_id == self.id:
            errors["parent"] = "A category cannot be its own parent."
        if self.parent_id and self.user_id:
            parent_user_id = (
                Category.objects.filter(pk=self.parent_id).values_list("user_id", flat=True).first()
            )
            if parent_user_id != self.user_id:
                errors["parent"] = "A category parent must belong to the same user."

        ancestor = self.parent
        visited: set[uuid.UUID] = set()
        while ancestor is not None:
            if ancestor.pk in visited or ancestor.pk == self.pk:
                errors["parent"] = "Category parents must not contain a cycle."
                break
            visited.add(ancestor.pk)
            ancestor = ancestor.parent

        if errors:
            raise ValidationError(errors)

    def ancestors(self) -> list[Category]:
        result: list[Category] = []
        parent = self.parent
        while parent is not None:
            result.append(parent)
            parent = parent.parent
        return result

    def __str__(self) -> str:
        return self.name_blind_index
