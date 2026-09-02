from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice


class TwoFactorRequiredMiddleware:
    """Keep enrolled users behind verification for every authenticated route."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        allowed = {reverse("login"), reverse("logout"), reverse("two-factor-verify")}
        user = request.user
        requires_verification = (
            user.is_authenticated
            and TOTPDevice.objects.filter(user=user, confirmed=True).exists()
            and not bool(getattr(user, "is_verified", lambda: False)())
        )
        static_path = f"/{settings.STATIC_URL.lstrip('/')}"
        if (
            requires_verification
            and request.path not in allowed
            and not request.path.startswith(static_path)
        ):
            request.session["two_factor_next"] = request.get_full_path()
            return redirect("two-factor-verify")
        return self.get_response(request)
