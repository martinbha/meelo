from django.contrib import admin

from .models import Category, CategoryRule, MerchantAlias


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "category_type", "parent", "is_system")
    list_filter = ("category_type", "is_system")
    search_fields = ("name_blind_index",)
    readonly_fields = ("name_encrypted",)


@admin.register(MerchantAlias)
class MerchantAliasAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("alias_blind_index", "normalized_merchant_blind_index", "default_category")
    search_fields = ("alias_blind_index", "normalized_merchant_blind_index")
    readonly_fields = ("alias_encrypted", "normalized_merchant_encrypted")


@admin.register(CategoryRule)
class CategoryRuleAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("merchant_pattern_blind_index", "category", "priority", "is_active")
    list_filter = ("is_active", "priority")
    search_fields = ("merchant_pattern_blind_index",)
    readonly_fields = ("merchant_pattern_encrypted",)
