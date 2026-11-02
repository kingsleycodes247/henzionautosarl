from django import forms
from django.contrib.auth import get_user_model

from .models import CompanySettings

User = get_user_model()

INPUT_CLASS = (
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm "
    "focus:border-pink-500 focus:ring-1 focus:ring-pink-500 outline-none"
)


class StyledFormMixin:
    """Adds Tailwind classes and HTML5 date pickers to every field."""

    def style_fields(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "h-4 w-4 rounded border-slate-300"
                continue
            if isinstance(field, forms.DateField) and not isinstance(field, forms.DateTimeField):
                field.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3
            field.widget.attrs["class"] = INPUT_CLASS


class StyledModelForm(StyledFormMixin, forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class StyledForm(StyledFormMixin, forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class CompanySettingsForm(StyledModelForm):
    class Meta:
        model = CompanySettings
        fields = [
            "name", "tagline", "description", "address", "phones", "niu",
            "boss_email", "boss_whatsapp", "vat_rate", "auto_approve_percent",
            "approval_timeout_minutes", "eod_start_hour", "eod_end_hour", "eod_auto_on_logout",
        ]

    def clean(self):
        data = super().clean()
        start, end = data.get("eod_start_hour"), data.get("eod_end_hour")
        if start is not None and end is not None and end <= start:
            self.add_error("eod_end_hour", "The window must end after it starts (for example 18 and 20).")
        return data


class UserForm(StyledModelForm):
    password = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password.",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "role", "is_active"]

    def clean(self):
        data = super().clean()
        if not self.instance.pk and not data.get("password"):
            self.add_error("password", "A password is required for new users.")
        if self.instance.pk and self.instance.is_owner and self.instance.is_active:
            from .users import other_active_owners

            demoted = data.get("role") != User.OWNER and not self.instance.is_superuser
            if (not data.get("is_active") or demoted) and other_active_owners(self.instance) == 0:
                raise forms.ValidationError("This is the only active boss account. Create or enable another boss first.")
        return data

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user
