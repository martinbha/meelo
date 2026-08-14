from django.contrib import admin

from apps.core.admin import OwnerScopedAdminMixin

from .models import FinancialAccount


@admin.register(FinancialAccount)
class FinancialAccountAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "account_type", "currency", "is_active")
    list_filter = ("account_type", "currency", "is_active")
    search_fields = ("name_blind_index", "institution_blind_index")
    readonly_fields = (
        "name_encrypted",
        "institution_encrypted",
        "masked_identifier_encrypted",
        "opening_balance_encrypted",
    )
