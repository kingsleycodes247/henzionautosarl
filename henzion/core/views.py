from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Sum
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, ListView, UpdateView

from core.constants import SALE_METHODS
from debts.services import debt_summary
from inventory.models import Product
from inventory.services import stock_valuation
from sales.models import DiscountRequest, Sale, SalePayment
from reports.services import eod_state
from sales.neighbors import owed_by_neighbor, profit_summary

from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from . import users as user_rules
from .forms import CompanySettingsForm, UserForm
from .models import AuditLog, CompanySettings, log
from .permissions import FormPageMixin, OwnerRequiredMixin, owner_required

User = get_user_model()


def _sales_by_method(sales_qs):
    """Money per method, from the payment lines, so a split sale counts under each method used."""
    rows = {
        r["method"]: r
        for r in SalePayment.objects.filter(sale__in=sales_qs)
        .values("method").annotate(n=Count("sale", distinct=True), total=Sum("amount"))
    }
    return [
        {"label": label, "key": key, "count": rows.get(key, {}).get("n", 0), "total": rows.get(key, {}).get("total", 0) or 0}
        for key, label in SALE_METHODS
    ]


@login_required
def dashboard(request):
    today = timezone.localdate()
    user = request.user
    done = Sale.objects.filter(status=Sale.COMPLETED)

    low = [p for p in Product.objects.with_stock().filter(is_active=True) if p.stock_qty <= p.reorder_level]
    ctx = {"today": today, "low_stock": low[:8], "low_stock_count": len(low)}

    if user.is_owner:
        today_qs = done.filter(date=today)
        month_qs = done.filter(date__year=today.year, date__month=today.month)
        now = timezone.now()
        pending = DiscountRequest.objects.filter(status=DiscountRequest.PENDING, expires_at__gt=now).select_related("product", "requested_by")
        ctx.update(
            # Sales = our own stock only. Items fetched from neighbors are left out; only their profit counts.
            today_total=today_qs.aggregate(s=Sum(F("total") - F("sourced_total")))["s"] or 0,
            today_count=today_qs.filter(total__gt=F("sourced_total")).count(),
            today_methods=_sales_by_method(today_qs),
            today_neighbor_money=today_qs.aggregate(s=Sum("sourced_total"))["s"] or 0,
            month_total=month_qs.aggregate(s=Sum(F("total") - F("sourced_total")))["s"] or 0,
            month_count=month_qs.filter(total__gt=F("sourced_total")).count(),
            profit_today=profit_summary(today, today),
            profit_month=profit_summary(today.replace(day=1), today),
            owed=owed_by_neighbor(),
            eod=eod_state(),
            pending=pending,
            debts=debt_summary(today),
            recent_sales=done.select_related("created_by")[:6],
            stock_value=stock_valuation()["totals"],
        )
        return render(request, "core/dashboard.html", ctx)

    mine = done.filter(date=today, created_by=user)
    ctx.update(
        today_total=mine.aggregate(s=Sum("total"))["s"] or 0,
        today_count=mine.count(),
        today_methods=_sales_by_method(mine),
        today_neighbor_money=mine.aggregate(s=Sum("sourced_total"))["s"] or 0,
        recent_sales=done.filter(created_by=user)[:6],
    )
    return render(request, "core/dashboard_cashier.html", ctx)


@owner_required
def company_settings(request):
    form = CompanySettingsForm(request.POST or None, instance=CompanySettings.get())
    if request.method == "POST" and form.is_valid():
        form.save()
        log(request.user, "settings updated", "Company settings", str(form.changed_data))
        messages.success(request, "Settings saved.")
        return redirect("company_settings")
    return render(request, "form.html", {
        "form": form, "page_title": "Company settings", "submit_label": "Save settings",
        "cancel_url": reverse_lazy("dashboard"),
        "subtitle": "These details appear on every invoice. The boss email and WhatsApp number receive discount approvals and reports.",
    })


class UserListView(OwnerRequiredMixin, ListView):
    model = User
    template_name = "core/user_list.html"
    context_object_name = "users"
    ordering = ["username"]


class UserCreateView(OwnerRequiredMixin, FormPageMixin, CreateView):
    model = User
    form_class = UserForm
    page_title = "New user"
    cancel_url = reverse_lazy("user_list")
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "user created", self.object.username, self.object.role)
        messages.success(self.request, "User created.")
        return response


class UserUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = User
    form_class = UserForm
    cancel_url = reverse_lazy("user_list")
    success_url = reverse_lazy("user_list")

    def get_page_title(self):
        return f"Edit user: {self.object.username}"

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "user updated", self.object.username, str(form.changed_data))
        messages.success(self.request, "User saved.")
        return response


@owner_required
def user_delete(request, pk):
    """Confirm, then delete. Sales and invoices stay and keep the person's name."""
    target = get_object_or_404(User, pk=pk)
    problem = user_rules.why_cannot_remove(target, request.user)
    if problem:
        messages.error(request, problem)
        return redirect("user_list")
    if request.method == "POST":
        name = target.username
        user_rules.delete_user_keeping_records(target)
        log(request.user, "user deleted", name, "records kept")
        messages.success(request, f"User {name} was deleted. Their sales and invoices are unchanged.")
        return redirect("user_list")
    n = user_rules.records_of(target)
    return render(request, "confirm_delete.html", {
        "object": f"{target.username} ({target.display_name()})",
        "warning": (
            f"{n['sales']} sale{'s' if n['sales'] != 1 else ''} and {n['payments']} debt payment{'s' if n['payments'] != 1 else ''} "
            "were made by this user. They stay exactly as they are, with this person's name kept on them. "
            "If they only left the shop, Disable is usually better: it blocks the login and changes nothing else."
        ),
        "cancel_url": reverse_lazy("user_list"),
    })


@owner_required
@require_POST
def user_toggle_active(request, pk):
    """Disable (cannot log in) or enable an account. Nothing else changes."""
    target = get_object_or_404(User, pk=pk)
    if target.is_active:
        problem = user_rules.why_cannot_remove(target, request.user)
        if problem:
            messages.error(request, problem)
            return redirect("user_list")
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    log(request.user, "user disabled" if not target.is_active else "user enabled", target.username)
    messages.success(request, f"{target.username} {'can log in again' if target.is_active else 'can no longer log in'}. Their past sales are unchanged.")
    return redirect("user_list")


class AuditListView(OwnerRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/audit_list.html"
    context_object_name = "entries"
    paginate_by = 50
    queryset = AuditLog.objects.select_related("user")
