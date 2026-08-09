from django.contrib import admin

from .models import CanonicalTransaction


@admin.register(CanonicalTransaction)
class CanonicalTransactionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("occurred_at", "transaction_type", "status", "currency", "user")
    list_filter = ("transaction_type", "status", "currency")
    search_fields = ("merchant_blind_index", "counterparty_blind_index")
    readonly_fields = (
        "amount_encrypted",
        "merchant_encrypted",
        "counterparty_encrypted",
        "notes_encrypted",
    )
