from django import forms

from core.forms import StyledModelForm

from .models import Customer, Debt, DebtPayment


class CustomerForm(StyledModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "whatsapp", "address", "id_number", "guarantor_name",
                  "guarantor_phone", "credit_limit", "notes"]


class DebtForm(StyledModelForm):
    class Meta:
        model = Debt
        fields = ["customer", "description", "total", "issued_on", "due_date", "notes"]

    def clean_total(self):
        total = self.cleaned_data["total"]
        if total <= 0:
            raise forms.ValidationError("The amount must be more than 0.")
        if self.instance.pk and total < self.instance.paid_amount:
            raise forms.ValidationError(
                f"Customer has already paid {self.instance.paid_amount} FCFA; the total can't be lower than that."
            )
        return total


class PaymentForm(StyledModelForm):
    class Meta:
        model = DebtPayment
        fields = ["amount", "method", "reference", "paid_on", "note"]

    def __init__(self, *args, debt=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.debt = debt or self.instance.debt

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("The amount must be more than 0.")
        paid_others = self.debt.paid_amount
        if self.instance.pk:
            paid_others -= DebtPayment.objects.get(pk=self.instance.pk).amount
        remaining = self.debt.total - paid_others
        if amount > remaining:
            raise forms.ValidationError(f"Too much: only {remaining} FCFA is still owed on this debt.")
        return amount
