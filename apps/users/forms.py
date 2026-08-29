from django import forms
from django.contrib.auth.forms import AuthenticationForm


class EmailAuthenticationForm(AuthenticationForm):
    """Authentication form that makes the private email identity explicit."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class TOTPConfirmationForm(forms.Form):
    token = forms.CharField(
        label="Authenticator code",
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class TOTPDisableForm(forms.Form):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )
