from django.contrib import admin

from .models import Category


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name_blind_index", "category_type", "parent", "is_system")
    list_filter = ("category_type", "is_system")
    search_fields = ("name_blind_index",)
