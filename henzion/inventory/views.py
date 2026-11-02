from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from core.models import log
from core.permissions import FormPageMixin, OwnerRequiredMixin, owner_required

from .forms import AdjustStockForm, CategoryForm, ProductForm, ReceiveStockForm
from .models import Category, Product, StockMovement
from .services import stock_valuation


class ProductListView(LoginRequiredMixin, ListView):
    template_name = "inventory/product_list.html"
    context_object_name = "products"
    paginate_by = 30

    def get_queryset(self):
        qs = Product.objects.with_stock().select_related("category")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q))
        if self.request.GET.get("low"):
            from django.db.models import F

            qs = qs.filter(stock_qty__lte=F("reorder_level"))
        if self.request.GET.get("show") != "all":
            qs = qs.filter(is_active=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["low"] = self.request.GET.get("low", "")
        ctx["show"] = self.request.GET.get("show", "")
        if self.request.user.is_owner:
            ctx["valuation"] = stock_valuation()["totals"]
        return ctx


class ProductCreateView(OwnerRequiredMixin, FormPageMixin, CreateView):
    model = Product
    form_class = ProductForm
    page_title = "New product"
    cancel_url = reverse_lazy("product_list")

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        qty = form.cleaned_data.get("initial_stock")
        if qty:
            StockMovement.objects.create(
                product=self.object, kind=StockMovement.OPENING, quantity=qty,
                note="Opening stock", created_by=self.request.user,
            )
        log(self.request.user, "product created", self.object)
        messages.success(self.request, f"{self.object.name} added.")
        return response

    def get_success_url(self):
        return reverse("product_list")


class ProductUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = Product
    form_class = ProductForm
    cancel_url = reverse_lazy("product_list")

    def get_page_title(self):
        return f"Edit {self.object.name}"

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "product updated", self.object, str(form.changed_data))
        messages.success(self.request, "Product updated.")
        return response

    def get_success_url(self):
        return reverse("product_list")


class ProductDeleteView(OwnerRequiredMixin, DeleteView):
    model = Product
    template_name = "confirm_delete.html"
    success_url = reverse_lazy("product_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["warning"] = "Its stock history is deleted too. Products that were already sold can only be hidden: untick 'Is active' when editing instead."
        ctx["cancel_url"] = reverse("product_list")
        return ctx

    def form_valid(self, form):
        from django.db.models import ProtectedError

        name = str(self.object)
        try:
            response = super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, "This product appears on past sales, so it can't be deleted. Mark it inactive instead.")
            return redirect("product_list")
        log(self.request.user, "product deleted", name)
        messages.success(self.request, "Product deleted.")
        return response


@owner_required
def stock_value(request):
    """The boss's view of what the goods in the shop are worth (cost prices stay hidden from cashiers)."""
    data = stock_valuation()
    q = request.GET.get("q", "").strip().lower()
    rows = data["rows"]
    if q:
        rows = [r for r in rows if q in r["product"].name.lower() or q in r["product"].sku.lower()]
    return render(request, "inventory/stock_value.html", {
        "rows": rows, "totals": data["totals"], "categories": data["categories"], "q": request.GET.get("q", ""),
    })


class CategoryListView(OwnerRequiredMixin, ListView):
    model = Category
    template_name = "inventory/category_list.html"
    context_object_name = "categories"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["form"] = kwargs.get("form") or CategoryForm()
        return ctx

    def post(self, request, *args, **kwargs):
        if "delete" in request.POST:
            Category.objects.filter(pk=request.POST["delete"]).delete()
            messages.success(request, "Category deleted.")
            return redirect("category_list")
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Category added.")
            return redirect("category_list")
        self.object_list = self.get_queryset()
        return self.render_to_response(self.get_context_data(form=form))


class MovementListView(LoginRequiredMixin, ListView):
    template_name = "inventory/movement_list.html"
    context_object_name = "movements"
    paginate_by = 40

    def get_queryset(self):
        qs = StockMovement.objects.select_related("product", "created_by")
        if self.request.GET.get("product"):
            qs = qs.filter(product_id=self.request.GET["product"])
        if self.request.GET.get("kind"):
            qs = qs.filter(kind=self.request.GET["kind"])
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["kinds"] = StockMovement.KIND_CHOICES
        ctx["product_id"] = self.request.GET.get("product", "")
        ctx["kind"] = self.request.GET.get("kind", "")
        return ctx


@owner_required
def receive_stock(request):
    form = ReceiveStockForm(request.POST or None, initial={"product": request.GET.get("product")})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            StockMovement.objects.create(
                product=d["product"], kind=StockMovement.RECEIPT, quantity=d["quantity"],
                supplier=d["supplier"], reference=d["reference"], created_by=request.user,
            )
            if d["cost_price"] is not None:
                d["product"].cost_price = d["cost_price"]
                d["product"].save(update_fields=["cost_price"])
        log(request.user, "stock received", d["product"], f"+{d['quantity']}")
        messages.success(request, f"Received {d['quantity']} × {d['product'].name}.")
        return redirect("product_list")
    return render(request, "form.html", {
        "form": form, "page_title": "Receive stock", "submit_label": "Add to stock",
        "cancel_url": reverse("product_list"),
    })


@owner_required
def adjust_stock(request):
    form = AdjustStockForm(request.POST or None, initial={"product": request.GET.get("product")})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        StockMovement.objects.create(
            product=d["product"], kind=StockMovement.ADJUSTMENT, quantity=d["quantity"],
            note=d["note"], created_by=request.user,
        )
        log(request.user, "stock adjusted", d["product"], f"{d['quantity']:+d}: {d['note']}")
        messages.success(request, "Stock adjusted.")
        return redirect("product_list")
    return render(request, "form.html", {
        "form": form, "page_title": "Adjust stock", "submit_label": "Save adjustment",
        "cancel_url": reverse("product_list"),
    })
