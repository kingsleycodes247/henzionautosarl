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
        fields = ["name", "sku", "category", "unit", "cost_price", "selling_price", "reorder_level", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            del self.fields["initial_stock"]


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
    quantity = forms.IntegerField(help_text="Use a negative number to remove stock (damage, loss, count correction).")
    note = forms.CharField(max_length=200, label="Reason")

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        if qty == 0:
            raise forms.ValidationError("Enter a number other than 0.")
        return qty
