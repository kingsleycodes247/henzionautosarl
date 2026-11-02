from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render


class OwnerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Only the boss (role=owner or superuser) may use the view."""

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_owner

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            return render(self.request, "403.html", status=403)
        return redirect_to_login(self.request.get_full_path())


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_owner:
            return render(request, "403.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper


class FormPageMixin:
    """Renders create/update views with the shared form.html template."""

    template_name = "form.html"
    page_title = ""
    cancel_url = "/"
    submit_label = "Save"

    def get_page_title(self):
        return self.page_title

    def get_cancel_url(self):
        return str(self.cancel_url)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            page_title=self.get_page_title(),
            cancel_url=self.get_cancel_url(),
            submit_label=self.submit_label,
        )
        return ctx
