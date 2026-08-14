from django.contrib import admin

from apps.core.admin import OwnerScopedAdminMixin

from .models import Category, CategoryRule, MerchantAlias


@admin.register(Category)
class CategoryAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "category_type", "parent", "is_system")
    list_filter = ("category_type", "is_system")
    search_fields = ("name_blind_index",)
    readonly_fields = ("name_encrypted",)

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(MerchantAlias)
class MerchantAliasAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("alias_blind_index", "normalized_merchant_blind_index", "default_category")
    search_fields = ("alias_blind_index", "normalized_merchant_blind_index")
    readonly_fields = ("alias_encrypted", "normalized_merchant_encrypted")


@admin.register(CategoryRule)
class CategoryRuleAdmin(OwnerScopedAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("merchant_pattern_blind_index", "category", "priority", "is_active")
    list_filter = ("is_active", "priority")
    search_fields = ("merchant_pattern_blind_index",)
    readonly_fields = ("merchant_pattern_encrypted",)
