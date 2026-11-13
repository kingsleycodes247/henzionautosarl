from django import forms

from core.forms import StyledForm, StyledModelForm

from .models import Category, Product, StockMovement


class ProductForm(StyledModelForm):
    initial_stock = forms.IntegerField(
        required=False, min_value=0, label="Opening stock",
        help_text="Quantity currently on the shelf (only used when creating).",
    )

    class Meta:
        model = Product
        fields = ["name", "sku", "category", "unit", "cost_price", "selling_price", "reorder_level",
                  "allow_half", "pieces_per_carton", "retail_version", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            del self.fields["initial_stock"]
        # the retail listing to choose from: any other product (usually named "... (RETAIL)")
        qs = Product.objects.filter(is_active=True)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["retail_version"].queryset = qs

    def clean(self):
        data = super().clean()
        if data.get("retail_version") and not data.get("pieces_per_carton"):
            self.add_error("pieces_per_carton", "Enter how many pieces one carton holds to link a retail listing.")
        if data.get("pieces_per_carton") and not data.get("retail_version"):
            self.add_error("retail_version", "Choose the retail listing that opened cartons go into.")
        return data


class CategoryForm(StyledModelForm):
    class Meta:
        model = Category
        fields = ["name"]


class ReceiveStockForm(StyledForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    quantity = forms.IntegerField(min_value=1)
    supplier = forms.CharField(required=False, max_length=120)
    reference = forms.CharField(required=False, max_length=60, label="Delivery note / reference")
    cost_price = forms.IntegerField(
        required=False, min_value=0, label="New cost price (FCFA)",
        help_text="Optional: update the product's cost price.",
    )


class AdjustStockForm(StyledForm):
    product = forms.ModelChoiceField(queryset=Product.objects.all())
    quantity = forms.DecimalField(
        max_digits=10, decimal_places=1,
        help_text="Use a negative number to remove stock (damage, loss, count correction). Half cartons: 0.5",
    )
    note = forms.CharField(max_length=200, label="Reason")

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        if qty == 0:
            raise forms.ValidationError("Enter a number other than 0.")
        if (qty * 2) % 1 != 0:
            raise forms.ValidationError("Use a whole number or a half (for example 2 or 2.5).")
        return qty

    def clean(self):
        data = super().clean()
        qty, product = data.get("quantity"), data.get("product")
        if qty is not None and product is not None and qty % 1 != 0 and not product.allow_half:
            self.add_error("quantity", f"{product.name} can't be counted in halves. Tick 'Can be sold by the half' on the product first.")
        return data
