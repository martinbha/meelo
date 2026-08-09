from django.contrib import admin

from .models import ChartOfAccounts, LedgerAccount, LedgerEntry


@admin.register(ChartOfAccounts)
class ChartOfAccountsAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "user", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name_blind_index",)


@admin.register(LedgerAccount)
class LedgerAccountAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("code", "name_blind_index", "account_type", "normal_balance", "is_active")
    list_filter = ("account_type", "normal_balance", "is_active", "is_system")
    search_fields = ("code", "name_blind_index")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("transaction", "account", "entry_type", "currency", "created_at")
    list_filter = ("entry_type", "currency")
    readonly_fields = ("amount_encrypted",)
