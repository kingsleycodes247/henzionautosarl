from django import forms

from core.forms import StyledModelForm

from .models import Neighbor, SaleItem


class NeighborItemForm(StyledModelForm):
    """The boss can correct what a neighbor charged, or record that he paid the neighbor.

    The description and the selling price are not editable here: they are on the customer's invoice.
    """

    class Meta:
        model = SaleItem
        fields = ["neighbor", "unit_cost", "neighbor_paid_on"]
        labels = {
            "unit_cost": "Cost per item: what the neighbor charges (FCFA)",
            "neighbor_paid_on": "Date you paid the neighbor",
        }
        help_texts = {"neighbor_paid_on": "Leave empty while you still owe the neighbor."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["neighbor"].required = True
        self.fields["neighbor"].empty_label = None

    def clean_unit_cost(self):
        cost = self.cleaned_data["unit_cost"]
        if cost < 0:
            raise forms.ValidationError("The cost can't be negative.")
        return cost


class NeighborForm(StyledModelForm):
    class Meta:
        model = Neighbor
        fields = ["name", "phone", "shop", "notes"]
