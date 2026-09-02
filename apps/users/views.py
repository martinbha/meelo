import base64
from typing import cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeView,
    PasswordResetConfirmView,
)
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import TemplateView
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from qrcode import QRCode
from qrcode.image.svg import SvgPathImage

from apps.core.audit import record_audit_event

from .forms import (
    EmailAuthenticationForm,
    TOTPConfirmationForm,
    TOTPDisableForm,
    TwoFactorVerificationForm,
)
from .models import User
from .recovery import consume_recovery_code, regenerate_recovery_codes
from .security import security_overview


class UserLoginView(LoginView):
    authentication_form = EmailAuthenticationForm
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        response = super().form_valid(form)
        record_audit_event(
            user=self.request.user,
            event_type="login_success",
            metadata={"method": "password"},
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT"),
        )
        if TOTPDevice.objects.filter(user=self.request.user, confirmed=True).exists():
            return redirect("two-factor-verify")
        return response

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        email = str(form.data.get("username", "")).strip()
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is not None:
            record_audit_event(
                user=user,
                event_type="login_failure",
                metadata={"method": "password"},
                ip_address=self.request.META.get("REMOTE_ADDR"),
                user_agent=self.request.META.get("HTTP_USER_AGENT"),
            )
        return super().form_invalid(form)


class UserLogoutView(LogoutView):
    next_page = settings.LOGOUT_REDIRECT_URL

    def post(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        if request.user.is_authenticated:
            record_audit_event(
                user=request.user,
                event_type="logout",
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT"),
            )
        return super().post(request, *args, **kwargs)


class UserPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password-change-done")

    def form_valid(self, form: object) -> HttpResponse:
        response = super().form_valid(form)
        record_audit_event(
            user=self.request.user,
            event_type="password_changed",
            metadata={"method": "authenticated_change"},
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT"),
        )
        return response


class UserPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("password-reset-complete")

    def form_valid(self, form: object) -> HttpResponse:
        user = form.user  # type: ignore[attr-defined]
        response = super().form_valid(form)
        record_audit_event(
            user=user,
            event_type="password_changed",
            metadata={"method": "reset_token"},
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT"),
        )
        return response


class AccountSecurityView(LoginRequiredMixin, TemplateView):
    """One page for the state of this account, and nothing about its money.

    Every action it links to lives somewhere else; this page exists so a person
    worried about their account has one place to look rather than four. What it
    deliberately does not have is a single financial figure — it is the page
    somebody opens *because* they are uneasy, often not alone.
    """

    template_name = "users/account_security.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        context["overview"] = security_overview(
            self.request.user,
            # Named so the page can say which session is the one being used to
            # read it. Only a prefix of any key is ever rendered.
            current_session_key=self.request.session.session_key or "",
        )
        return context


def _enrollment_device(user: object) -> TOTPDevice:
    device = TOTPDevice.objects.filter(user=user, confirmed=False).order_by("id").first()
    return device or TOTPDevice.objects.create(user=user, name="authenticator", confirmed=False)


def _qr_svg(device: TOTPDevice) -> str:
    qr = QRCode(border=2)
    qr.add_data(device.config_url)
    qr.make(fit=True)
    return qr.make_image(image_factory=SvgPathImage).to_string(encoding="unicode")


def _enrollment_context(device: TOTPDevice, form: TOTPConfirmationForm) -> dict[str, object]:
    return {
        "form": form,
        "manual_secret": base64.b32encode(device.bin_key).decode("ascii").rstrip("="),
        "qr_svg": _qr_svg(device),
    }


class TOTPEnrollView(LoginRequiredMixin, View):
    template_name = "users/totp_enroll.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        device = _enrollment_device(request.user)
        return render(
            request,
            self.template_name,
            _enrollment_context(device, TOTPConfirmationForm()),
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        device = _enrollment_device(request.user)
        form = TOTPConfirmationForm(request.POST)
        if form.is_valid() and device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=("confirmed",))
            record_audit_event(user=request.user, event_type="two_factor_enabled", obj=device)
            codes = regenerate_recovery_codes(cast(User, request.user))
            return render(request, "users/recovery_codes.html", {"codes": codes})
        if form.is_valid():
            form.add_error("token", "The code is incorrect or expired.")
        return render(
            request,
            self.template_name,
            _enrollment_context(device, form),
            status=400,
        )


class TOTPDisableView(LoginRequiredMixin, View):
    template_name = "users/totp_disable.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, {"form": TOTPDisableForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = TOTPDisableForm(request.POST)
        if form.is_valid() and request.user.check_password(form.cleaned_data["password"]):
            deleted, _ = TOTPDevice.objects.filter(user=request.user).delete()
            record_audit_event(
                user=request.user,
                event_type="two_factor_disabled",
                metadata={"devices_removed": deleted},
            )
            return redirect("account-security")
        if form.is_valid():
            form.add_error("password", "The password is incorrect.")
        return render(request, self.template_name, {"form": form}, status=400)


class RecoveryCodeRegenerateView(LoginRequiredMixin, View):
    template_name = "users/recovery_codes.html"

    def post(self, request: HttpRequest) -> HttpResponse:
        codes = regenerate_recovery_codes(cast(User, request.user))
        return render(request, self.template_name, {"codes": codes})


class TwoFactorVerifyView(LoginRequiredMixin, View):
    template_name = "users/two_factor_verify.html"
    failure_limit = 5
    lock_seconds = 300

    def get(self, request: HttpRequest) -> HttpResponse:
        if bool(getattr(request.user, "is_verified", lambda: False)()):
            return redirect(request.session.pop("two_factor_next", settings.LOGIN_REDIRECT_URL))
        return render(request, self.template_name, {"form": TwoFactorVerificationForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        ip = request.META.get("REMOTE_ADDR", "unknown")
        key = f"two-factor-failures:{user.pk}:{ip}"
        failures = int(cache.get(key, 0))
        if failures >= self.failure_limit:
            return render(
                request,
                self.template_name,
                {"form": TwoFactorVerificationForm(), "throttled": True},
                status=429,
            )

        form = TwoFactorVerificationForm(request.POST)
        device = TOTPDevice.objects.filter(user=user, confirmed=True).order_by("id").first()
        token = form.cleaned_data["token"].strip() if form.is_valid() else ""
        verified = bool(device and token and device.verify_token(token))
        if not verified and token:
            verified = consume_recovery_code(user, token)
        if verified and device is not None:
            cache.delete(key)
            otp_login(request, device)
            return redirect(request.session.pop("two_factor_next", settings.LOGIN_REDIRECT_URL))

        cache.set(key, failures + 1, self.lock_seconds)
        record_audit_event(
            user=user,
            event_type="two_factor_failure",
            metadata={"method": "otp"},
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT"),
        )
        if form.is_valid():
            form.add_error("token", "The code is incorrect or expired.")
        return render(request, self.template_name, {"form": form}, status=400)
