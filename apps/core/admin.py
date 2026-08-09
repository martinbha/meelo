from django.contrib import admin

from .models import AuditEvent


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
