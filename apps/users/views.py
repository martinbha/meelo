from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeView,
    PasswordResetConfirmView,
)
from django.http import HttpRequest, HttpResponse
from django.urls import reverse_lazy

from apps.core.audit import record_audit_event

from .forms import EmailAuthenticationForm


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
        return response


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
