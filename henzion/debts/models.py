from django.conf import settings
from django.db import models
from django.db.models import BigIntegerField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.constants import PAYMENT_METHODS


class Customer(models.Model):
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)
    whatsapp = models.CharField("WhatsApp number", max_length=30, blank=True)
    address = models.CharField(max_length=200, blank=True)
    id_number = models.CharField("ID card number", max_length=40, blank=True)
    guarantor_name = models.CharField("Guarantor / reference person", max_length=120, blank=True)
    guarantor_phone = models.CharField(max_length=30, blank=True)
    credit_limit = models.BigIntegerField("Credit limit (FCFA)", default=0, help_text="0 = no limit.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    def outstanding(self):
        return sum(d.balance_amount for d in Debt.objects.with_balance().filter(customer=self))


class DebtQuerySet(models.QuerySet):
    def with_balance(self):
        """Adds paid_sum and balance_sum so lists need only one query."""
        return self.annotate(
            paid_sum=Coalesce(Sum("payments__amount"), Value(0), output_field=BigIntegerField())
        ).annotate(balance_sum=F("total") - F("paid_sum"))


class Debt(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="debts")
    sale = models.OneToOneField("sales.Sale", null=True, blank=True, on_delete=models.SET_NULL, related_name="debt")
    description = models.CharField(max_length=200, help_text="What the customer took, or the invoice number.")
    total = models.BigIntegerField("Amount owed (FCFA)")
    issued_on = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = DebtQuerySet.as_manager()

    class Meta:
        ordering = ["-issued_on", "-id"]

    def __str__(self):
        return f"{self.customer.name}: {self.description}"

    @property
    def paid_amount(self):
        value = getattr(self, "paid_sum", None)
        if value is None:
            value = self.payments.aggregate(s=Sum("amount"))["s"] or 0
        return value

    @property
    def balance_amount(self):
        return self.total - self.paid_amount

    @property
    def is_paid(self):
        return self.balance_amount <= 0

    @property
    def is_overdue(self):
        return not self.is_paid and bool(self.due_date) and self.due_date < timezone.localdate()

    @property
    def status(self):
        if self.is_paid:
            return "paid"
        if self.is_overdue:
            return "overdue"
        return "partial" if self.paid_amount > 0 else "open"

    @property
    def status_label(self):
        return {"paid": "Paid", "overdue": "Overdue", "partial": "Part paid", "open": "Open"}[self.status]


class DebtPayment(models.Model):
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="payments")
    amount = models.BigIntegerField("Amount paid (FCFA)")
    method = models.CharField(max_length=14, choices=PAYMENT_METHODS, default="cash")
    reference = models.CharField("Mobile money reference", max_length=60, blank=True)
    paid_on = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    received_by_name = models.CharField(max_length=150, blank=True, help_text="Kept if the user account is deleted.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    @property
    def receiver_name(self):
        if self.received_by_id:
            return self.received_by.display_name()
        return self.received_by_name or "Former user"

    def __str__(self):
        return f"{self.amount} FCFA on {self.paid_on}"
