from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from .models import AuditEvent


class OwnerScopedAdminMixin:
    """Keep non-superuser admin reads and relationship choices within one owner."""

    def get_queryset(self, request: HttpRequest) -> models.QuerySet:  # type: ignore[type-arg]
        queryset = super().get_queryset(request)  # type: ignore[misc]
        if request.user.is_superuser:
            return queryset
        return queryset.filter(user=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):  # type: ignore[no-untyped-def]
        related_model = db_field.remote_field.model
        if (
            not request.user.is_superuser
            and related_model is not None
            and any(field.name == "user" for field in related_model._meta.fields)
        ):
            kwargs["queryset"] = related_model.objects.filter(user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)  # type: ignore[misc]

    def save_model(self, request, obj, form, change):  # type: ignore[no-untyped-def]
        if not request.user.is_superuser and hasattr(obj, "user_id"):
            obj.user = request.user
        super().save_model(request, obj, form, change)  # type: ignore[misc]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("created_at", "event_type", "object_type", "request_id")
    list_filter = ("event_type", "created_at")
    search_fields = ("object_type", "request_id", "digest")
    readonly_fields = (
        "user",
        "event_type",
        "object_type",
        "object_id",
        "request_id",
        "ip_hash",
        "user_agent_hash",
        "metadata",
        "previous_digest",
        "digest",
        "created_at",
    )
