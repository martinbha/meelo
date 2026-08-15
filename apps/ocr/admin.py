from django.contrib import admin

from apps.core.admin import OwnerScopedAdminMixin

from .models import OcrRun


@admin.register(OcrRun)
class OcrRunAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "engine",
        "engine_version",
        "succeeded",
        "error_code",
        "duration_ms",
        "created_at",
    )
    list_filter = ("engine", "succeeded", "error_code")
    readonly_fields = (
        "user",
        "source_document",
        "engine",
        "engine_version",
        "languages",
        "configuration_encrypted",
        "preprocessing_encrypted",
        "selected_preprocessing_variant",
        "raw_output_encrypted",
        "succeeded",
        "error_code",
        "duration_ms",
        "started_at",
        "completed_at",
        "created_at",
    )

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
