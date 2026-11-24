"""Forms for authentication and HAProxy backend/server management."""
from django import forms
from django.contrib.auth import authenticate, get_user_model

from .models import Backend, haproxy_name_validator

User = get_user_model()


class LoginForm(forms.Form):
    """Authenticates a user by either email address or username."""

    identifier = forms.CharField(
        label="Email or Username",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "placeholder": "admin@haproxy.local or admin",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "current-password",
                "placeholder": "Enter your password",
            }
        ),
    )

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        identifier = (cleaned_data.get("identifier") or "").strip()
        password = cleaned_data.get("password")

        if identifier and password:
            username = identifier
            if "@" in identifier:
                matched_user = User.objects.filter(email__iexact=identifier).first()
                if matched_user:
                    username = matched_user.get_username()

            self.user_cache = authenticate(
                self.request,
                username=username,
                password=password,
            )
            if self.user_cache is None:
                raise forms.ValidationError("Invalid email/username or password.")
            if not self.user_cache.is_active:
                raise forms.ValidationError("This account is inactive.")

        return cleaned_data

    def get_user(self):
        return self.user_cache


class CreateBackendForm(forms.Form):
    name = forms.CharField(
        label="Backend Name",
        max_length=255,
        validators=[haproxy_name_validator],
    )
    mode = forms.ChoiceField(
        label="Mode",
        choices=Backend.Mode.choices,
        initial=Backend.Mode.HTTP,
    )
    balance_algorithm = forms.ChoiceField(
        label="Balance Algorithm",
        choices=Backend.BalanceAlgorithm.choices,
        initial=Backend.BalanceAlgorithm.ROUNDROBIN,
    )
    description = forms.CharField(
        label="Description",
        max_length=255,
        required=False,
    )

    def clean_name(self) -> str:
        return self.cleaned_data["name"].strip()


class CreateServerForm(forms.Form):
    name = forms.CharField(
        label="Server Name",
        max_length=255,
        validators=[haproxy_name_validator],
    )
    address = forms.CharField(
        label="Address",
        max_length=255,
    )
    port = forms.IntegerField(
        label="Port",
        min_value=1,
        max_value=65535,
        initial=80,
    )
    check = forms.ChoiceField(
        label="Health Check",
        choices=[("enabled", "Enabled"), ("disabled", "Disabled")],
        initial="enabled",
    )

    def clean_name(self) -> str:
        return self.cleaned_data["name"].strip()

    def clean_address(self) -> str:
        return self.cleaned_data["address"].strip()


class RenameServerForm(forms.Form):
    old_name = forms.CharField(max_length=255, validators=[haproxy_name_validator])
    new_name = forms.CharField(max_length=255, validators=[haproxy_name_validator])

    def clean_old_name(self) -> str:
        return self.cleaned_data["old_name"].strip()

    def clean_new_name(self) -> str:
        return self.cleaned_data["new_name"].strip()


class ServerStateForm(forms.Form):
    VALID_STATES = [
        ("ready", "Ready"),
        ("drain", "Drain"),
        ("maint", "Maintenance"),
    ]
    server_name = forms.CharField(max_length=255, validators=[haproxy_name_validator])
    admin_state = forms.ChoiceField(choices=VALID_STATES)

    def clean_server_name(self) -> str:
        return self.cleaned_data["server_name"].strip()
