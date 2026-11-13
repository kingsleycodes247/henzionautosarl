import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import ListView, UpdateView

from core.constants import METHOD_LABELS, SALE_METHODS
from core.models import CompanySettings, log
from core.qty import num_qty
from core.permissions import FormPageMixin, OwnerRequiredMixin, owner_required
from inventory.models import Product

from . import services
from .forms import NeighborForm, NeighborItemForm
from .models import DiscountRequest, Neighbor, Proforma, Sale, SaleItem
from .neighbors import owed_by_neighbor, sourced_lines


def _safe_next(request, default):
    """Where to go back to after a button press, but only to a page on this site."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return reverse(default)


# --------------------------------------------------------------------------- POS

@login_required
def pos(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "Bad request."}, status=400)
        try:
            sale = services.create_sale(request.user, data)
        except services.SaleError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "redirect": reverse("sale_detail", args=[sale.pk])})

    all_products = list(Product.objects.with_stock().select_related("category").filter(is_active=True))
    # retail listing id -> the carton product that can be opened to refill it
    cartons = {
        c.retail_version_id: {"id": c.pk, "name": c.name, "pieces": c.pieces_per_carton, "stock": num_qty(c.stock_qty)}
        for c in all_products if c.retail_version_id and c.pieces_per_carton
    }
    products = [
        {"id": p.pk, "name": p.name, "sku": p.sku, "price": p.selling_price,
         "stock": num_qty(p.stock_qty), "unit": p.unit, "category": p.category.name if p.category else "",
         "half": p.allow_half, "carton": cartons.get(p.pk)}
        for p in all_products
    ]
    cs = CompanySettings.get()
    prefill = _pos_prefill(request, {p["id"]: p for p in products})
    return render(request, "sales/pos.html", {
        "products": products,
        "prefill": prefill,
        "neighbors": list(Neighbor.objects.values_list("name", flat=True)),
        "methods": SALE_METHODS,
        "is_owner": request.user.is_owner,
        "vat_rate": float(cs.vat_rate),
        "today": timezone.localdate().isoformat(),
    })


def _pos_prefill(request, products_by_id):
    """Loads an unpaid invoice into the till. ?proforma=ID = its money has arrived, honour its prices;
    ?copy=ID = start a new cart from it at today's prices (for an expired or cancelled one)."""
    pay = bool(request.GET.get("proforma"))
    raw_id = request.GET.get("proforma") or request.GET.get("copy") or ""
    if not raw_id.isdigit():
        return None
    pf = Proforma.objects.prefetch_related("items").filter(pk=int(raw_id)).first()
    if pf is None:
        messages.error(request, "That unpaid invoice was not found.")
        return None
    if pay and not pf.can_be_paid:
        why = "has expired" if pf.is_expired else f"is {pf.get_status_display().lower()}"
        messages.error(request, f"{pf.number} {why}, so it can't be paid. Use 'Copy into a new sale' instead.")
        return None
    items, sourced, skipped = [], [], []
    for it in pf.items.all():
        if it.is_sourced:
            sourced.append({"name": it.description, "neighbor": it.neighbor_name, "cost": it.unit_cost, "price": it.net_price, "qty": num_qty(it.quantity)})
            continue
        product = products_by_id.get(it.product_id)
        if product is None:
            skipped.append(it.description)
            continue
        items.append({
            "product_id": it.product_id, "item_id": it.pk if pay else None, "qty": num_qty(it.quantity),
            "price": it.net_price if pay else product["price"],
        })
    return {
        "mode": "pay" if pay else "copy", "id": pf.pk, "number": pf.number, "notes": pf.notes,
        "customer": {"name": pf.customer_name, "phone": pf.customer_phone, "address": pf.customer_address, "niu": pf.customer_niu},
        "items": items, "sourced": sourced, "skipped": skipped,
    }


# --------------------------------------------------------------------------- unpaid invoices (proforma)

@login_required
@require_POST
def proforma_create(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "Bad request."}, status=400)
    try:
        pf = services.create_proforma(request.user, data)
    except services.SaleError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "redirect": reverse("proforma_detail", args=[pf.pk])})


@login_required
def proforma_list(request):
    qs = Proforma.objects.all()
    status = request.GET.get("status", "open")
    today = timezone.localdate()
    if status == "open":
        qs = qs.filter(status=Proforma.OPEN, valid_until__gte=today)
    elif status == "expired":
        qs = qs.filter(status=Proforma.OPEN, valid_until__lt=today)
    elif status in (Proforma.PAID, Proforma.CANCELLED):
        qs = qs.filter(status=status)
    else:
        status = "all"
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q))
    page = Paginator(qs.select_related("sale"), 30).get_page(request.GET.get("page"))
    waiting = Proforma.objects.filter(status=Proforma.OPEN, valid_until__gte=today)
    from django.db.models import Sum

    return render(request, "sales/proforma_list.html", {
        "page_obj": page, "paginator": page.paginator, "is_paginated": page.has_other_pages(), "proformas": page,
        "status": status, "q": q, "waiting_count": waiting.count(), "waiting_total": waiting.aggregate(s=Sum("total"))["s"] or 0,
    })


@login_required
def proforma_detail(request, pk):
    pf = get_object_or_404(Proforma.objects.select_related("sale", "created_by"), pk=pk)
    return render(request, "sales/proforma_detail.html", {"pf": pf, "items": pf.items.all()})


@login_required
def proforma_invoice(request, pk):
    """The printable unpaid invoice (same design as the paper invoice, marked UNPAID)."""
    pf = get_object_or_404(Proforma, pk=pk)
    items = list(pf.items.all())
    return render(request, "sales/invoice.html", {
        "sale": pf, "is_proforma": True, "items": items, "payments": [], "debt": None,
        "blank_rows": range(len(items) + 1, 9), "company": CompanySettings.get(),
    })


@login_required
@require_POST
def proforma_toggle_prices(request, pk):
    pf = get_object_or_404(Proforma, pk=pk)
    pf.hide_prices = not pf.hide_prices
    pf.save(update_fields=["hide_prices"])
    messages.success(request, "Prices are now hidden on the printed copy." if pf.hide_prices else "Prices are now shown on the printed copy.")
    return redirect("proforma_detail", pk=pf.pk)


@login_required
@require_POST
def proforma_cancel(request, pk):
    pf = get_object_or_404(Proforma, pk=pk)
    if not request.user.is_owner and pf.created_by_id != request.user.pk:
        return render(request, "403.html", status=403)
    try:
        services.cancel_proforma(request.user, pf)
        messages.success(request, f"{pf.number} cancelled. Stock was never touched.")
    except services.SaleError as exc:
        messages.error(request, str(exc))
    return redirect("proforma_detail", pk=pf.pk)


# --------------------------------------------------------------------------- sales list / detail

class SaleListView(ListView):
    template_name = "sales/sale_list.html"
    context_object_name = "sales"
    paginate_by = 30

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = Sale.objects.select_related("created_by")
        if not self.request.user.is_owner:
            qs = qs.filter(created_by=self.request.user)
        g = self.request.GET
        if g.get("q"):
            qs = qs.filter(Q(number__icontains=g["q"]) | Q(customer_name__icontains=g["q"]) | Q(customer_phone__icontains=g["q"]))
        if g.get("method"):
            qs = qs.filter(payments__method=g["method"]).distinct()  # also finds split sales that include it
        if g.get("from"):
            qs = qs.filter(date__gte=g["from"])
        if g.get("to"):
            qs = qs.filter(date__lte=g["to"])
        if g.get("status"):
            qs = qs.filter(status=g["status"])
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["methods"] = SALE_METHODS
        ctx["f"] = self.request.GET
        return ctx


def _get_sale_for_user(request, pk):
    sale = get_object_or_404(Sale.objects.select_related("created_by"), pk=pk)
    if not request.user.is_owner and sale.created_by_id != request.user.pk:
        return None
    return sale


@login_required
def sale_detail(request, pk):
    sale = _get_sale_for_user(request, pk)
    if sale is None:
        return render(request, "403.html", status=403)
    from debts.models import Debt

    debt = Debt.objects.with_balance().filter(sale=sale).first()
    return render(request, "sales/sale_detail.html", {
        "sale": sale, "items": sale.items.all(), "debt": debt, "payments": sale.payments.all(),
        "method_label": METHOD_LABELS[sale.payment_method],
    })


@login_required
def invoice(request, pk):
    sale = _get_sale_for_user(request, pk)
    if sale is None:
        return render(request, "403.html", status=403)
    from debts.models import Debt

    items = list(sale.items.all())
    return render(request, "sales/invoice.html", {
        "sale": sale, "items": items, "payments": list(sale.payments.all()),
        "blank_rows": range(len(items) + 1, 9),  # always at least 8 numbered rows, like the paper invoice
        "debt": Debt.objects.with_balance().filter(sale=sale).first(),
        "method_label": METHOD_LABELS[sale.payment_method],
        "company": CompanySettings.get(),
    })


@owner_required
@require_POST
def sale_cancel(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    services.cancel_sale(request.user, sale)
    messages.success(request, f"{sale.number} cancelled. Stock was returned.")
    return redirect("sale_detail", pk=sale.pk)


# --------------------------------------------------------------------------- discount approval

@login_required
@require_POST
def discount_request(request):
    try:
        data = json.loads(request.body or b"{}")
        product = get_object_or_404(Product, pk=int(data["product_id"]), is_active=True)
        req = services.request_discount(
            request.user, product, int(data["price"]), Decimal(str(data.get("quantity") or 1)), data.get("reason", ""),
        )
    except services.SaleError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return JsonResponse({"ok": False, "error": "Bad request."}, status=400)
    return JsonResponse({
        "ok": True, "id": req.pk, "status": req.status,
        "expires_in": max(0, int((req.expires_at - timezone.now()).total_seconds())),
    })


@login_required
def discount_status(request, pk):
    req = get_object_or_404(DiscountRequest, pk=pk, requested_by=request.user)
    return JsonResponse({"id": req.pk, "status": req.refresh()})


def discount_decide(request, token):
    """Public page opened from the WhatsApp/email link. Needs a button press (POST) so that
    link previews and email scanners can't approve anything by merely loading the URL."""
    cs = CompanySettings.get()
    try:
        req = services.read_token(token, cs.approval_timeout_minutes * 60 + 60)
    except signing.SignatureExpired:
        return render(request, "sales/discount_decide.html", {"problem": "This link has expired. The original price stays."})
    except (signing.BadSignature, DiscountRequest.DoesNotExist):
        return render(request, "sales/discount_decide.html", {"problem": "This link is not valid."})

    req.refresh()
    if request.method == "POST" and req.status == DiscountRequest.PENDING:
        approve = request.POST.get("action") == "approve"
        services.decide_discount(req, approve, "link")
        req.refresh_from_db()
    return render(request, "sales/discount_decide.html", {"req": req, "pending": req.status == DiscountRequest.PENDING})


class DiscountListView(OwnerRequiredMixin, ListView):
    template_name = "sales/discount_list.html"
    context_object_name = "requests"
    paginate_by = 30
    queryset = DiscountRequest.objects.select_related("product", "requested_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        pending = list(DiscountRequest.objects.filter(status=DiscountRequest.PENDING).select_related("product", "requested_by"))
        ctx["pending"] = [r for r in pending if r.refresh() == DiscountRequest.PENDING]
        return ctx


@owner_required
@require_POST
def discount_decide_dashboard(request, pk):
    req = get_object_or_404(DiscountRequest, pk=pk)
    ok = services.decide_discount(req, request.POST.get("action") == "approve", "dashboard")
    messages.success(request, "Decision saved.") if ok else messages.warning(request, "That request was already answered or has expired.")
    return redirect(_safe_next(request, "discount_list"))


# --------------------------------------------------------------------------- neighbor items (boss only)

def _date_or(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


@owner_required
def neighbor_items(request):
    """Every item the cashiers fetched from neighbors: what it cost, what it sold for, profit or loss,
    and whether the neighbor has been paid."""
    g = request.GET
    today = timezone.localdate()
    start = _date_or(g.get("from"), today.replace(day=1))
    end = _date_or(g.get("to"), today)

    qs = sourced_lines().select_related("sale", "sale__created_by", "neighbor").filter(sale__date__range=(start, end))
    if g.get("neighbor", "").isdigit():
        qs = qs.filter(neighbor_id=int(g["neighbor"]))
    status = g.get("status", "all")
    if status == "unpaid":
        qs = qs.filter(neighbor_paid_on__isnull=True)
    elif status == "paid":
        qs = qs.filter(neighbor_paid_on__isnull=False)
    rows = list(qs.order_by("-sale__date", "-id"))
    if g.get("loss"):
        rows = [r for r in rows if r.profit < 0]

    totals = {
        "qty": sum(r.quantity for r in rows),
        "revenue": sum(r.amount for r in rows),
        "cost": sum(r.cost_total for r in rows),
        "profit": sum(r.profit for r in rows),
        "loss": sum(-r.profit for r in rows if r.profit < 0),
        "unpaid": sum(r.cost_total for r in rows if r.neighbor_paid_on is None),
    }
    paginator = Paginator(rows, 50)
    page = paginator.get_page(g.get("page"))
    return render(request, "sales/neighbor_items.html", {
        "page_obj": page, "paginator": paginator, "is_paginated": page.has_other_pages(),
        "totals": totals, "owed": owed_by_neighbor(), "neighbors": Neighbor.objects.all(),
        "start": start, "end": end, "status": status, "neighbor_id": g.get("neighbor", ""), "loss": g.get("loss", ""),
    })


@owner_required
@require_POST
def neighbor_item_paid(request, pk):
    """Flip one item between 'still owed to the neighbor' and 'paid'."""
    item = get_object_or_404(SaleItem.objects.select_related("neighbor"), pk=pk, is_sourced=True)
    item.neighbor_paid_on = None if item.neighbor_paid_on else timezone.localdate()
    item.save(update_fields=["neighbor_paid_on"])
    state = "paid" if item.neighbor_paid_on else "unpaid again"
    log(request.user, "neighbor item " + state, item.description, f"{item.neighbor} {item.cost_total} FCFA")
    return redirect(_safe_next(request, "neighbor_items"))


@owner_required
@require_POST
def neighbor_settle(request, pk):
    """The boss pays a neighbor what he owes: every unpaid item of that neighbor becomes paid."""
    neighbor = get_object_or_404(Neighbor, pk=pk)
    items = list(sourced_lines().filter(neighbor=neighbor, neighbor_paid_on__isnull=True))
    SaleItem.objects.filter(pk__in=[i.pk for i in items]).update(neighbor_paid_on=timezone.localdate())
    owed = sum(i.cost_total for i in items)
    log(request.user, "neighbor settled", neighbor, f"{len(items)} item(s), {owed} FCFA")
    messages.success(request, f"{neighbor.name}: {len(items)} item{'s' if len(items) != 1 else ''} marked as paid ({services.money(owed)} FCFA).")
    return redirect(_safe_next(request, "neighbor_items"))


class NeighborItemUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    queryset = SaleItem.objects.filter(is_sourced=True)
    form_class = NeighborItemForm
    cancel_url = "/sales/neighbors/"

    def get_page_title(self):
        return f"Edit neighbor item: {self.object.description}"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subtitle"] = (
            f"Sold on {self.object.sale.number} for {services.money(self.object.unit_price)} FCFA each. "
            "Fix the neighbor's price if the cashier typed it wrong; profit is recalculated."
        )
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "neighbor item updated", self.object.description, str(form.changed_data))
        messages.success(self.request, "Neighbor item updated.")
        return response

    def get_success_url(self):
        return reverse("neighbor_items")


class NeighborUpdateView(OwnerRequiredMixin, FormPageMixin, UpdateView):
    model = Neighbor
    form_class = NeighborForm
    cancel_url = "/sales/neighbors/"

    def get_page_title(self):
        return f"Edit neighbor: {self.object.name}"

    def form_valid(self, form):
        response = super().form_valid(form)
        log(self.request.user, "neighbor updated", self.object.name, str(form.changed_data))
        messages.success(self.request, "Neighbor saved.")
        return response

    def get_success_url(self):
        return reverse("neighbor_items")
