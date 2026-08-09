from django.contrib import admin

from .models import ProcessingJob


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("created_at", "task_name", "status", "attempt_count", "user")
    list_filter = ("status", "task_name")
    search_fields = ("task_name", "last_error_code")
    readonly_fields = (
        "id",
        "user",
        "document_id",
        "task_name",
        "payload",
        "status",
        "attempt_count",
        "max_attempts",
        "available_at",
        "locked_at",
        "started_at",
        "completed_at",
        "last_error_code",
        "last_error_message",
        "created_at",
        "updated_at",
    )
