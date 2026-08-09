from django.contrib import admin

from .models import PaymentInstrument


@admin.register(PaymentInstrument)
class PaymentInstrumentAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "instrument_type", "last_four", "is_active")
    list_filter = ("instrument_type", "is_active")
    search_fields = ("name_blind_index",)
    readonly_fields = ("name_encrypted", "issuer_encrypted")
