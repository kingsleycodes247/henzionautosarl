from django.contrib import admin

from .models import Category, Product, StockMovement

admin.site.register(Category)
admin.site.register(StockMovement)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "selling_price", "is_active")
    search_fields = ("name", "sku")
