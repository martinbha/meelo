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
from django.http import HttpRequest, HttpResponse
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from apps.core.audit import record_audit_event

from .forms import EmailAuthenticationForm
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
