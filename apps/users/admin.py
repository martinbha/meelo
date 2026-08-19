from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):  # type: ignore[type-arg]
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active", "date_joined")
    search_fields = ("email",)
    readonly_fields = ("date_joined", "last_login", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Security", {"fields": ("encryption_key_version",)}),
        ("Dates", {"fields": ("last_login", "date_joined", "created_at", "updated_at")}),
    )


# django-otp registers its own admin, and its TOTP form exposes the device's
# shared secret as an editable field. Anyone who can reach the admin can then
# read the seed and generate valid codes from anywhere, forever — which makes
# the second factor worth nothing while looking entirely present. The same goes
# for a static device's tokens, which are the recovery codes.
#
# Re-registered here with the secret removed rather than merely made read-only:
# a read-only field is still rendered.
admin.site.unregister(TOTPDevice)
admin.site.unregister(StaticDevice)
if admin.site.is_registered(StaticToken):  # pragma: no cover - registration varies
    admin.site.unregister(StaticToken)


class DeviceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """What a device is, without what it knows."""

    list_display = ("name", "user", "confirmed")
    list_filter = ("confirmed",)
    search_fields = ("user__email",)
    raw_id_fields = ("user",)
    #: Whether a device exists and whether it is confirmed is administrable.
    #: Its seed is not, by anybody, through here.
    fields = ("user", "name", "confirmed")


@admin.register(TOTPDevice)
class TOTPDeviceAdmin(DeviceAdmin):
    pass


@admin.register(StaticDevice)
class StaticDeviceAdmin(DeviceAdmin):
    pass
