from django.conf import settings
from django.contrib.auth.views import LoginView, LogoutView

from .forms import EmailAuthenticationForm


class UserLoginView(LoginView):
    authentication_form = EmailAuthenticationForm
    template_name = "registration/login.html"
    redirect_authenticated_user = True


class UserLogoutView(LogoutView):
    next_page = settings.LOGOUT_REDIRECT_URL
