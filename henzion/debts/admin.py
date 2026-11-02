from django.contrib import admin

from .models import Customer, Debt, DebtPayment


class PaymentInline(admin.TabularInline):
    model = DebtPayment
    extra = 0


@admin.register(Debt)
class DebtAdmin(admin.ModelAdmin):
    list_display = ("customer", "description", "total", "due_date")
    inlines = [PaymentInline]


admin.site.register(Customer)
