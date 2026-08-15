from django.contrib import admin

from apps.core.admin import OwnerScopedAdminMixin

from .models import OcrRun, OcrToken


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
        "model_versions",
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


@admin.register(OcrToken)
class OcrTokenAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("ocr_run", "sequence", "confidence", "line_number", "word_number")
    readonly_fields = (
        "user",
        "ocr_run",
        "text_encrypted",
        "normalized_text_encrypted",
        "confidence",
        "left",
        "top",
        "right",
        "bottom",
        "page_number",
        "block_number",
        "paragraph_number",
        "line_number",
        "word_number",
        "sequence",
        "created_at",
    )

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
