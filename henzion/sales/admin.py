from django.contrib import admin

from .models import DiscountRequest, Neighbor, Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "customer_name", "payment_method", "total", "status")
    inlines = [SaleItemInline]


admin.site.register(DiscountRequest)
admin.site.register(Neighbor)
