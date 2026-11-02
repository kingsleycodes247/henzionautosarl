from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.models import CompanySettings, log
from core.permissions import FormPageMixin, OwnerRequiredMixin

from .forms import CustomerForm, DebtForm, PaymentForm
from .models import Customer, Debt, DebtPayment
from .services import debt_summary


# --------------------------------------------------------------------------- debts register

class DebtListView(LoginRequiredMixin, ListView):
    template_name = "debts/debt_list.html"
    context_object_name = "debts"
    paginate_by = 30

    def get_queryset(self):
        qs = Debt.objects.with_balance().select_related("customer", "sale")
        g = self.request.GET
        status = g.get("status", "open")
        today = timezone.localdate()
        if status == "open":
            qs = qs.filter(balance_sum__gt=0)
        elif status == "overdue":
            qs = qs.filter(balance_sum__gt=0, due_date__lt=today)
        elif status == "paid":
            qs = qs.filter(balance_sum__lte=0)
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(customer__name__icontains=q) | Q(customer__phone__icontains=q) | Q(description__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status"] = self.request.GET.get("status", "open")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["summary"] = debt_summary()
        return ctx


class DebtDetailView(LoginRequiredMixin, DetailView):
    template_name = "debts/debt_detail.html"
    context_object_name = "debt"
    queryset = Debt.objects.with_balance().select_related("customer", "sale")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        debt = self.object
        ctx["payments"] = debt.payments.select_related("received_by")
        if not debt.is_paid:
            ctx["payment_form"] = PaymentForm(debt=debt, initial={"amount": debt.balance_amount})
        return ctx


class DebtCreateView(OwnerRequiredMixin, FormPageMixin, CreateView):
    model = Debt
    form_class = DebtForm
    page_title = "Record a debt"
    cancel_url = reverse_lazy("debt_list")

    def get_initial(self):
        initial = super().get_initial()
        if self.request.GET.get("customer"):
            initial["customer"] = self.request.GET["customer"]
        return initial

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        log(self.request.user, "debt created", self.object, f"{self.object.total} FCFA")
        messages.success(self.request, "Debt recorded.")
        return response

    def get_success_url(self):
        return reverse("debt_detail", args=[self.object.pk])


class DebtUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = Debt
    form_class = DebtForm

    def get_page_title(self):
        return f"Edit debt: {self.object.customer.name}"

    def get_cancel_url(self):
        return reverse("debt_detail", args=[self.object.pk])

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "debt updated", self.object, str(form.changed_data))
        messages.success(self.request, "Debt updated.")
        return response

    def get_success_url(self):
        return reverse("debt_detail", args=[self.object.pk])


class DebtDeleteView(OwnerRequiredMixin, DeleteView):
    model = Debt
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("debt_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        n = self.object.payments.count()
        ctx["warning"] = f"This also deletes its {n} payment(s). The sale/invoice itself is not affected."
        ctx["cancel_url"] = reverse("debt_detail", args=[self.object.pk])
        return ctx

    def form_valid(self, form):
        label = f"{self.object} ({self.object.total} FCFA)"
        response = super().form_valid(form)
        log(self.request.user, "debt deleted", label)
        messages.success(self.request, "Debt deleted.")
        return response


# --------------------------------------------------------------------------- payments

@login_required
def payment_create(request, pk):
    debt = get_object_or_404(Debt.objects.select_related("customer"), pk=pk)
    if debt.is_paid:
        messages.info(request, "This debt is already fully paid.")
        return redirect("debt_detail", pk=debt.pk)
    form = PaymentForm(request.POST or None, debt=debt, initial={"amount": debt.balance_amount})
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.debt = debt
        payment.received_by = request.user
        payment.received_by_name = request.user.display_name()
        payment.save()
        log(request.user, "payment recorded", debt, f"{payment.amount} FCFA {payment.method}")
        if debt.is_paid:
            messages.success(request, f"Payment saved. {debt.customer.name} has now paid in full!")
        else:
            messages.success(request, f"Payment saved. {debt.balance_amount} FCFA still owed.")
        return redirect("payment_receipt", pk=payment.pk)
    return render(request, "form.html", {
        "form": form, "page_title": f"Record payment: {debt.customer.name}",
        "subtitle": f"{debt.description} - still owed: {debt.balance_amount} FCFA",
        "submit_label": "Save payment", "cancel_url": reverse("debt_detail", args=[debt.pk]),
    })


class PaymentUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = DebtPayment
    form_class = PaymentForm
    page_title = "Edit payment"

    def get_cancel_url(self):
        return reverse("debt_detail", args=[self.object.debt_id])

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "payment updated", self.object.debt, str(form.changed_data))
        messages.success(self.request, "Payment updated.")
        return response

    def get_success_url(self):
        return reverse("debt_detail", args=[self.object.debt_id])


class PaymentDeleteView(OwnerRequiredMixin, DeleteView):
    model = DebtPayment
    template_name = "confirm_delete.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["warning"] = "The debt balance goes back up by this amount."
        ctx["cancel_url"] = reverse("debt_detail", args=[self.object.debt_id])
        return ctx

    def form_valid(self, form):
        debt_id = self.object.debt_id
        label = f"{self.object.amount} FCFA on {self.object.debt}"
        self.success_url = reverse("debt_detail", args=[debt_id])
        response = super().form_valid(form)
        log(self.request.user, "payment deleted", label)
        messages.success(self.request, "Payment deleted.")
        return response


@login_required
def payment_receipt(request, pk):
    payment = get_object_or_404(DebtPayment.objects.select_related("debt__customer", "received_by"), pk=pk)
    debt = Debt.objects.with_balance().get(pk=payment.debt_id)
    return render(request, "debts/receipt.html", {
        "payment": payment, "debt": debt, "company": CompanySettings.get(),
    })


# --------------------------------------------------------------------------- customers

class CustomerListView(LoginRequiredMixin, ListView):
    template_name = "debts/customer_list.html"
    context_object_name = "customers"
    paginate_by = 30

    def get_queryset(self):
        qs = Customer.objects.all()
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = "debts/customer_detail.html"
    context_object_name = "customer"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        debts = list(Debt.objects.with_balance().filter(customer=self.object))
        ctx["debts"] = debts
        ctx["owed"] = sum(d.balance_amount for d in debts)
        ctx["paid"] = sum(d.paid_amount for d in debts)
        return ctx


class CustomerCreateView(OwnerRequiredMixin, FormPageMixin, CreateView):
    model = Customer
    form_class = CustomerForm
    page_title = "New customer"
    cancel_url = reverse_lazy("customer_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "customer created", self.object)
        messages.success(self.request, "Customer added.")
        return response

    def get_success_url(self):
        return reverse("customer_detail", args=[self.object.pk])


class CustomerUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = Customer
    form_class = CustomerForm

    def get_page_title(self):
        return f"Edit {self.object.name}"

    def get_cancel_url(self):
        return reverse("customer_detail", args=[self.object.pk])

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "customer updated", self.object, str(form.changed_data))
        messages.success(self.request, "Customer updated.")
        return response

    def get_success_url(self):
        return reverse("customer_detail", args=[self.object.pk])


class CustomerDeleteView(OwnerRequiredMixin, DeleteView):
    model = Customer
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("customer_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        n = self.object.debts.count()
        ctx["warning"] = f"This also deletes {n} debt(s) and all their payments."
        ctx["cancel_url"] = reverse("customer_detail", args=[self.object.pk])
        return ctx

    def form_valid(self, form):
        label = str(self.object)
        response = super().form_valid(form)
        log(self.request.user, "customer deleted", label)
        messages.success(self.request, "Customer deleted.")
        return response
