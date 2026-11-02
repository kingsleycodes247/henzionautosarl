from django.conf import settings
from django.db import models
from django.db.models import BigIntegerField, Sum, Value
from django.db.models.functions import Coalesce


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class ProductQuerySet(models.QuerySet):
    def with_stock(self):
        """Adds `stock_qty` = sum of every stock movement (the ledger)."""
        return self.annotate(
            stock_qty=Coalesce(Sum("movements__quantity"), Value(0), output_field=BigIntegerField())
        )


class Product(models.Model):
    name = models.CharField(max_length=160)
    sku = models.CharField("Code / SKU", max_length=40, unique=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="products")
    unit = models.CharField(max_length=20, default="pcs")
    cost_price = models.BigIntegerField("Cost price (FCFA)", default=0)
    selling_price = models.BigIntegerField("Selling price (FCFA)")
    reorder_level = models.PositiveIntegerField("Low-stock alert at", default=5)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProductQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def stock(self):
        value = getattr(self, "stock_qty", None)
        if value is not None:
            return value
        return self.movements.aggregate(s=Sum("quantity"))["s"] or 0

    @property
    def is_low(self):
        return self.stock <= self.reorder_level


class StockMovement(models.Model):
    """Append-only stock ledger: quantity on hand is the sum of these rows."""

    RECEIPT = "receipt"
    SALE = "sale"
    RETURN = "return"
    ADJUSTMENT = "adjustment"
    OPENING = "opening"
    KIND_CHOICES = [
        (OPENING, "Opening stock"),
        (RECEIPT, "Stock received"),
        (SALE, "Sale"),
        (RETURN, "Sale cancelled / return"),
        (ADJUSTMENT, "Adjustment"),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="movements")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    quantity = models.IntegerField(help_text="Positive = stock in, negative = stock out.")
    reference = models.CharField(max_length=60, blank=True)
    supplier = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.product} {self.quantity:+d} ({self.kind})"
