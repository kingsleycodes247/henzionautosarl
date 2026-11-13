"""Business rules for sales. Views stay thin; everything that touches money lives here."""
import datetime
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core import signing
from django.db import transaction
from django.db.models import Sum
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.conf import settings

from core import notify, push
from core.constants import CREDIT, METHOD_LABELS, SALE_METHODS, SPLIT
from core.models import CompanySettings, log
from core.qty import TENTH, fmt_qty, line_money, num_qty
from debts.models import Customer, Debt
from inventory.models import Product, StockMovement

from .models import DiscountRequest, Neighbor, Proforma, ProformaItem, Sale, SaleItem, SalePayment

TOKEN_SALT = "henzion.discount"
APPROVAL_VALID_FOR = timedelta(hours=2)  # an approved price can be used on a sale for 2 hours


class SaleError(Exception):
    """A problem the cashier can fix; the message is shown on screen."""


def money(n):
    return f"{int(n):,}".replace(",", " ")


def _to_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SaleError(f"Invalid {label}.")
    return number


def _to_qty(value, product):
    """A quantity: a whole number, or a half (0.5, 1.5 ...) for products that can be sold by the half."""
    try:
        q = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SaleError(f"Invalid quantity for {product.name}.")
    if q <= 0:
        raise SaleError(f"Quantity for {product.name} must be more than 0.")
    if (q * 2) % 1 != 0:
        raise SaleError(f"Quantity for {product.name} must be a whole number or a half.")
    if q % 1 != 0 and not product.allow_half:
        raise SaleError(f"{product.name} can't be sold by the half.")
    return q.quantize(TENTH)


def _carton_source(product):
    """The carton product whose cartons are opened into this retail product (if it has one)."""
    return Product.objects.filter(retail_version=product, pieces_per_carton__isnull=False, is_active=True).first()


def _line_amounts(price, list_price, qty):
    """(unit_price, discount, amount) for one line. Anything below the list price is a discount."""
    amount = line_money(price, qty)
    unit_price = max(price, list_price)
    discount = line_money(list_price, qty) - amount if price < list_price else 0
    return unit_price, discount, amount


def stock_of(product_id):
    return StockMovement.objects.filter(product_id=product_id).aggregate(s=Sum("quantity"))["s"] or 0


# --------------------------------------------------------------------------- discounts

def decide_discount(req, approve, via):
    """Approve/decline a pending request. Returns False if it was already decided or expired."""
    with transaction.atomic():
        req = DiscountRequest.objects.select_for_update().get(pk=req.pk)
        if req.refresh() != DiscountRequest.PENDING:
            return False
        req.status = DiscountRequest.APPROVED if approve else DiscountRequest.DECLINED
        req.decided_via = via
        req.decided_at = timezone.now()
        req.save(update_fields=["status", "decided_via", "decided_at"])
    log(None, f"discount {req.status}", req.product, f"{req.original_price}->{req.requested_price} via {via}")
    if via not in ("owner", "auto-rule"):
        _push_to_cashier(req, approve)
    return True


def make_token(req):
    return signing.dumps({"id": req.pk}, salt=TOKEN_SALT)


def read_token(token, max_age_seconds):
    data = signing.loads(token, salt=TOKEN_SALT, max_age=max_age_seconds)
    return DiscountRequest.objects.select_related("product", "requested_by").get(pk=data["id"])


def _build_boss_message(req, cs):
    """The approval message for the boss: plain text (WhatsApp / fallback) and a styled HTML email.

    Built in the web request, so the background thread only handles plain strings."""
    link = f"{settings.SITE_URL}{reverse('discount_decide', args=[make_token(req)])}"
    who = req.requested_by.display_name() if req.requested_by else "A cashier"
    text = (
        f"{cs.name} - price reduction request\n"
        f"Product: {req.product.name} (qty {fmt_qty(req.quantity)})\n"
        f"List price: {money(req.original_price)} FCFA\n"
        f"Requested: {money(req.requested_price)} FCFA (-{req.percent}%)\n"
        f"By: {who}\n"
        + (f"Note: {req.reason}\n" if req.reason else "")
        + f"Approve or decline: {link}\n"
        f"Valid for {cs.approval_timeout_minutes} min. No answer = price stays the same."
    )
    html = render_to_string("emails/discount_request.html", {
        "company": cs.name, "who": who, "product": req.product.name, "quantity": num_qty(req.quantity),
        "original": money(req.original_price), "requested": money(req.requested_price),
        "percent": req.percent, "saving": money(line_money(req.original_price - req.requested_price, req.quantity)),
        "reason": req.reason, "link": link, "minutes": cs.approval_timeout_minutes,
    })
    subject = (
        f"Approval needed: {req.product.name}, {money(req.original_price)} → "
        f"{money(req.requested_price)} FCFA"
    )
    return {"text": text, "html": html, "subject": subject}


def _deliver_to_boss(message, whatsapp, email):
    """Runs in a background thread: only plain strings, no database access."""
    if whatsapp:
        notify.send_whatsapp(whatsapp, message["text"])
    if email:
        notify.send_email(message["subject"], message["text"], email, html=message["html"])
    if not whatsapp and not email:
        print(f"\n[Discount request - no boss contact configured in Settings]\n{message['text']}\n")


def _push_to_boss(req):
    """Phone notification for the boss(es): tapping it opens the signed Approve/Decline page."""
    who = req.requested_by.display_name() if req.requested_by else "A cashier"
    push.notify_users(
        push.owners(),
        "Price approval needed",
        f"{who}: {req.product.name} {money(req.original_price)} → {money(req.requested_price)} FCFA (-{req.percent}%)",
        url=reverse("discount_decide", args=[make_token(req)]),
        tag=f"discount-{req.pk}",
    )


def _push_to_cashier(req, approved):
    if req.requested_by_id is None:
        return
    word = "approved" if approved else "declined"
    push.notify_users(
        [req.requested_by],
        f"Price {word}",
        f"{req.product.name}: {money(req.requested_price)} FCFA " + ("approved by the boss." if approved else "declined, the original price stays."),
        url=reverse("pos"),
        tag=f"discount-{req.pk}",
    )


def _push_sale_to_boss(user, sale, breakdown, has_credit):
    """Optional 'you just made a sale' alert. Never for the boss's own sales; off unless he turned it on."""
    cs = CompanySettings.get()
    if user.is_owner or not cs.notify_every_sale:
        return
    if sale.total < cs.notify_sale_min and not has_credit:
        return
    push.notify_users(
        push.owners(), f"Sale: {money(sale.total)} FCFA",
        f"{user.display_name()} · {breakdown}" + (" · includes credit" if has_credit else ""),
        url=reverse("sale_detail", args=[sale.pk]), tag=f"sale-{sale.pk}",
    )


def request_discount(user, product, price, quantity, reason=""):
    cs = CompanySettings.get()
    original = product.selling_price
    if price < 0 or price >= original:
        raise SaleError("The requested price must be lower than the list price.")
    quantity = max(Decimal("0.5"), Decimal(str(quantity)))
    req = DiscountRequest.objects.create(
        product=product, quantity=quantity, original_price=original, requested_price=price,
        reason=reason[:200], requested_by=user,
        expires_at=timezone.now() + timedelta(minutes=cs.approval_timeout_minutes),
    )
    if user.is_owner:
        decide_discount(req, True, "owner")
    elif cs.auto_approve_percent and req.percent <= cs.auto_approve_percent:
        decide_discount(req, True, "auto-rule")
    else:
        notify.run_in_background(_deliver_to_boss, _build_boss_message(req, cs), cs.boss_whatsapp, cs.boss_email)
        _push_to_boss(req)
    req.refresh_from_db()
    return req


# --------------------------------------------------------------------------- sales

def _parse_date(value, label):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise SaleError(f"Enter a valid {label}.")


def _parse_payments(data):
    """How the customer pays: a list of {'method', 'amount', 'reference'}.

    The POS sends `payments`: one entry per method used (cash, mtn_momo, orange_money, credit).
    An old-style payload with a single `payment_method` is still accepted; its amount is None,
    meaning "the whole total", and is filled in once the total is known.
    """
    allowed = dict(SALE_METHODS)
    raw = data.get("payments")
    if not raw:
        method = data.get("payment_method")
        if method not in allowed:
            raise SaleError("Choose how the customer is paying.")
        return [{"method": method, "amount": None, "reference": (data.get("payment_reference") or "").strip()[:60]}]

    merged = {}
    for p in raw:
        method = p.get("method")
        if method not in allowed:
            raise SaleError("Unknown payment method.")
        amount = _to_int(p.get("amount") or 0, "payment amount")
        if amount < 0:
            raise SaleError("Payment amounts can't be negative.")
        if amount == 0:
            continue
        entry = merged.setdefault(method, {"method": method, "amount": 0, "reference": ""})
        entry["amount"] += amount
        entry["reference"] = entry["reference"] or (p.get("reference") or "").strip()[:60]
    if not merged:
        raise SaleError("Enter how much the customer is paying.")
    return list(merged.values())


def _parse_sourced(raw_list):
    """Items the shop doesn't stock: fetched from a neighbor and resold to the customer.

    Each is {'name', 'neighbor', 'quantity', 'cost' (what the neighbor charges), 'price' (what the
    customer pays)}. They never touch stock and are not part of the shop's own sales.
    """
    lines = []
    for raw in raw_list:
        name = (raw.get("name") or "").strip()[:200]
        neighbor = (raw.get("neighbor") or "").strip()[:120]
        if not name:
            raise SaleError("Enter the name of the item taken from a neighbor.")
        if not neighbor:
            raise SaleError(f"Enter which neighbor {name} came from.")
        qty = _to_int(raw.get("quantity"), "quantity")
        cost = _to_int(raw.get("cost"), "neighbor's price")
        price = _to_int(raw.get("price"), "selling price")
        if qty < 1:
            raise SaleError(f"Quantity for {name} must be at least 1.")
        if cost < 0:
            raise SaleError(f"Invalid neighbor price for {name}.")
        if price <= 0:
            raise SaleError(f"Enter the selling price for {name}.")
        lines.append(dict(name=name, neighbor=neighbor, qty=qty, cost=cost, price=price, amount=price * qty))
    return lines


def _get_neighbor(name):
    """The neighbor with this name (any capitalisation), created the first time it is used."""
    return Neighbor.objects.filter(name__iexact=name).first() or Neighbor.objects.create(name=name)


def _authorise_price(user, product, price, raw, used_requests):
    """Anyone may sell at or above the list price; a cashier's lower price needs the boss's approval."""
    list_price = product.selling_price
    if price < list_price and not user.is_owner:
        req = DiscountRequest.objects.select_for_update().filter(
            pk=_to_int(raw.get("request_id") or 0, "approval"), product=product,
            requested_by=user, status=DiscountRequest.APPROVED, consumed=False,
            decided_at__gte=timezone.now() - APPROVAL_VALID_FOR,
        ).first()
        if not req or req.requested_price != price or req.pk in used_requests:
            raise SaleError(f"The reduced price for {product.name} needs the boss's approval.")
        used_requests.add(req.pk)
    # A price above the list price is allowed for everyone; only a lower price needs approval.


def _totals(subtotal, cs):
    vat = int((Decimal(subtotal) * cs.vat_rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return vat, subtotal + vat


@transaction.atomic
def create_sale(user, data):
    """Validate a POS payload and create the sale, its payment lines, stock movements and,
    for any part paid on credit, the debt. Items from neighbors are on the invoice but outside stock
    and outside the shop's own sales."""
    client_uuid = data.get("client_uuid") or None
    if client_uuid:
        existing = Sale.objects.filter(client_uuid=client_uuid).first()
        if existing:  # same sale submitted twice -> return the first one
            return existing

    cs = CompanySettings.get()
    raw_items = data.get("items") or []
    sourced = _parse_sourced(data.get("sourced_items") or [])
    if not raw_items and not sourced:
        raise SaleError("Add at least one product.")

    # Paying an unpaid invoice (proforma): its quoted prices are honoured, and it is marked paid below.
    proforma, quoted, used_quoted = None, {}, set()
    if data.get("proforma_id"):
        proforma = Proforma.objects.select_for_update().filter(pk=_to_int(data["proforma_id"], "unpaid invoice")).first()
        if not proforma:
            raise SaleError("That unpaid invoice no longer exists.")
        if proforma.status == Proforma.PAID:
            raise SaleError(f"{proforma.number} has already been paid.")
        if proforma.status == Proforma.CANCELLED:
            raise SaleError(f"{proforma.number} was cancelled.")
        if proforma.is_expired:
            raise SaleError(f"{proforma.number} expired on {proforma.valid_until:%d/%m/%Y}. Create a new one.")
        quoted = {pi.pk: pi for pi in proforma.items.filter(is_sourced=False)}

    parts = _parse_payments(data)
    has_credit = any(p["method"] == CREDIT for p in parts)

    cust = data.get("customer") or {}
    c_name = (cust.get("name") or "").strip()
    c_phone = (cust.get("phone") or "").strip()
    c_address = (cust.get("address") or "").strip()

    due_date = None
    if has_credit:
        if not (c_name and c_phone and c_address):
            raise SaleError("Credit sales need the customer's name, phone number and address.")
        due_date = _parse_date(data.get("due_date"), "due date")
        if due_date < timezone.localdate():
            raise SaleError("The due date can't be in the past.")

    ids = sorted({_to_int(i.get("product_id"), "product") for i in raw_items})
    products = {p.pk: p for p in Product.objects.select_for_update().filter(pk__in=ids, is_active=True)}

    lines, needed, used_requests = [], {}, set()
    for raw in raw_items:
        pid = _to_int(raw.get("product_id"), "product")
        price = _to_int(raw.get("price"), "price")
        product = products.get(pid)
        if not product:
            raise SaleError("A product on this sale no longer exists.")
        qty = _to_qty(raw.get("quantity"), product)
        if price < 0:
            raise SaleError(f"Invalid price for {product.name}.")

        list_price = product.selling_price
        pi = quoted.get(_to_int(raw.get("proforma_item_id") or 0, "unpaid invoice item")) if raw.get("proforma_item_id") else None
        honoured = bool(
            pi and pi.product_id == pid and price == pi.net_price and qty <= pi.quantity and pi.pk not in used_quoted
        )
        if honoured:  # the price was already authorised when the unpaid invoice was made
            used_quoted.add(pi.pk)
        else:
            _authorise_price(user, product, price, raw, used_requests)

        unit_price, discount, amount = _line_amounts(price, list_price, qty)
        needed[pid] = needed.get(pid, 0) + qty
        lines.append(dict(product=product, qty=qty, unit_price=unit_price, discount=discount,
                          amount=amount, request_id=raw.get("request_id")))

    for pid, qty in needed.items():
        available = stock_of(pid)
        if qty > available:
            raise SaleError(
                f"Not enough stock for {products[pid].name}: {fmt_qty(available)} available, {fmt_qty(qty)} requested."
                + (" Open a carton to move stock to the shelf." if _carton_source(products[pid]) else "")
            )

    sourced_subtotal = sum(s["amount"] for s in sourced)
    subtotal = sum(line["amount"] for line in lines) + sourced_subtotal
    vat, total = _totals(subtotal, cs)
    if total <= 0:
        raise SaleError("The sale total must be more than 0.")
    # VAT is shared out in proportion, so own sales + neighbor items always add back up to the invoice.
    sourced_vat = int((Decimal(vat) * sourced_subtotal / subtotal).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sourced_total = sourced_subtotal + sourced_vat

    # The payment lines must add up to exactly the total (server-side, whatever the browser says).
    for p in parts:
        if p["amount"] is None:
            p["amount"] = total
    paid = sum(p["amount"] for p in parts)
    if paid != total:
        raise SaleError(
            f"The payments add up to {money(paid)} FCFA but the sale total is {money(total)} FCFA."
        )
    credit_amount = sum(p["amount"] for p in parts if p["method"] == CREDIT)
    method = parts[0]["method"] if len(parts) == 1 else SPLIT

    customer = None
    if has_credit:
        customer = Customer.objects.filter(phone=c_phone).first() or Customer(phone=c_phone)
        customer.name = c_name
        customer.address = c_address
        customer.whatsapp = (cust.get("whatsapp") or customer.whatsapp or "").strip()
        customer.id_number = (cust.get("id_number") or customer.id_number or "").strip()
        customer.guarantor_name = (cust.get("guarantor_name") or customer.guarantor_name or "").strip()
        customer.guarantor_phone = (cust.get("guarantor_phone") or customer.guarantor_phone or "").strip()
        if customer.pk and customer.credit_limit and customer.outstanding() + credit_amount > customer.credit_limit:
            raise SaleError(
                f"Credit limit exceeded: {customer.name} already owes {money(customer.outstanding())} FCFA "
                f"(limit {money(customer.credit_limit)} FCFA)."
            )
        customer.save()

    sale = Sale.objects.create(
        client_uuid=client_uuid, due_date=due_date,
        customer_name=c_name, customer_phone=c_phone, customer_address=c_address,
        customer_niu=(cust.get("niu") or "").strip(),
        payment_method=method, payment_reference=next((p["reference"] for p in parts if p["reference"]), ""),
        notes=(data.get("notes") or "").strip()[:255],
        subtotal=subtotal, vat_amount=vat, total=total, sourced_total=sourced_total, created_by=user, created_by_name=user.display_name(),
    )
    for line in lines:
        SaleItem.objects.create(
            sale=sale, product=line["product"], description=line["product"].name,
            quantity=line["qty"], unit_price=line["unit_price"], discount=line["discount"],
            amount=line["amount"], unit_cost=line["product"].cost_price,
        )
        StockMovement.objects.create(
            product=line["product"], kind=StockMovement.SALE, quantity=-line["qty"],
            reference=sale.number, created_by=user,
        )
    for s in sourced:  # on the invoice like any item, but no product, no stock movement
        SaleItem.objects.create(
            sale=sale, product=None, description=s["name"], quantity=s["qty"], unit_price=s["price"],
            discount=0, amount=s["amount"], unit_cost=s["cost"], is_sourced=True,
            neighbor=_get_neighbor(s["neighbor"]),
        )
    DiscountRequest.objects.filter(pk__in=used_requests).update(consumed=True)
    if proforma:
        proforma.status, proforma.sale = Proforma.PAID, sale
        proforma.save(update_fields=["status", "sale"])

    for p in parts:
        SalePayment.objects.create(sale=sale, method=p["method"], amount=p["amount"], reference=p["reference"])

    if has_credit:
        # Only the credit part is owed; what was paid in cash / mobile money is already settled.
        Debt.objects.create(
            customer=customer, sale=sale,
            description=f"Invoice {sale.number}" + (" (credit part)" if method == SPLIT else ""),
            total=credit_amount, issued_on=sale.date, due_date=due_date, created_by=user,
        )
    breakdown = " + ".join(f"{METHOD_LABELS[p['method']]} {money(p['amount'])}" for p in parts)
    extra = f" (incl. {len(sourced)} neighbor item{'s' if len(sourced) != 1 else ''}: {money(sourced_total)})" if sourced else ""
    from_pf = f" (paid unpaid invoice {proforma.number})" if proforma else ""
    log(user, "sale created", sale, f"{money(total)} FCFA = {breakdown}{extra}{from_pf}")
    _push_sale_to_boss(user, sale, breakdown, has_credit)
    return sale


@transaction.atomic
def cancel_sale(user, sale):
    """Puts the goods back into stock and removes the debt that came with a credit sale."""
    if sale.status == Sale.CANCELLED:
        return
    for item in sale.items.select_related("product"):
        if item.is_sourced:
            continue  # neighbor items never touched our stock, so there is nothing to put back
        StockMovement.objects.create(
            product=item.product, kind=StockMovement.RETURN, quantity=item.quantity,
            reference=sale.number, note="Sale cancelled", created_by=user,
        )
    Debt.objects.filter(sale=sale).delete()  # a cancelled credit sale leaves no debt behind
    already_paid = sale.items.filter(is_sourced=True, neighbor_paid_on__isnull=False).count()
    sale.status = Sale.CANCELLED
    sale.save(update_fields=["status"])
    note = f" ({already_paid} neighbor item(s) had already been paid for)" if already_paid else ""
    log(user, "sale cancelled", sale, f"{money(sale.total)} FCFA{note}")
    cs = CompanySettings.get()
    if cs.notify_cancellations and not user.is_owner:
        push.notify_users(
            push.owners(), "Sale cancelled",
            f"{user.display_name()} cancelled {sale.number} ({money(sale.total)} FCFA).",
            url=reverse("sale_detail", args=[sale.pk]), tag=f"cancel-{sale.pk}",
        )


@transaction.atomic
def create_proforma(user, data):
    """An unpaid invoice to give to a customer or company. It takes nothing out of stock, is not a
    sale and is counted nowhere until it is paid (see create_sale with `proforma_id`)."""
    client_uuid = data.get("client_uuid") or None
    if client_uuid:
        existing = Proforma.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing

    cs = CompanySettings.get()
    raw_items = data.get("items") or []
    sourced = _parse_sourced(data.get("sourced_items") or [])
    if not raw_items and not sourced:
        raise SaleError("Add at least one product.")
    cust = data.get("customer") or {}
    c_name = (cust.get("name") or "").strip()
    if not c_name:
        raise SaleError("Enter the customer or company name for the unpaid invoice.")
    raw_days = data.get("valid_days")
    days = 7 if raw_days in (None, "") else _to_int(raw_days, "number of days")
    if not 1 <= days <= 90:
        raise SaleError("An unpaid invoice can be valid for 1 to 90 days.")

    ids = sorted({_to_int(i.get("product_id"), "product") for i in raw_items})
    products = {p.pk: p for p in Product.objects.select_for_update().select_related("category").filter(pk__in=ids, is_active=True)}

    lines, used_requests = [], set()
    for raw in raw_items:
        pid = _to_int(raw.get("product_id"), "product")
        price = _to_int(raw.get("price"), "price")
        product = products.get(pid)
        if not product:
            raise SaleError("A product on this invoice no longer exists.")
        qty = _to_qty(raw.get("quantity"), product)
        if price < 0:
            raise SaleError(f"Invalid price for {product.name}.")
        _authorise_price(user, product, price, raw, used_requests)
        list_price = product.selling_price
        unit_price, discount, amount = _line_amounts(price, list_price, qty)
        lines.append(dict(product=product, qty=qty, unit_price=unit_price, list_price=list_price, discount=discount,
                          amount=amount))

    subtotal = sum(l["amount"] for l in lines) + sum(s["amount"] for s in sourced)
    vat, total = _totals(subtotal, cs)
    if total <= 0:
        raise SaleError("The total must be more than 0.")

    today = timezone.localdate()
    pf = Proforma.objects.create(
        client_uuid=client_uuid, date=today, valid_until=today + timedelta(days=days),
        customer_name=c_name[:120], customer_phone=(cust.get("phone") or "").strip()[:30],
        customer_address=(cust.get("address") or "").strip()[:200], customer_niu=(cust.get("niu") or "").strip()[:40],
        notes=(data.get("notes") or "").strip()[:255], hide_prices=bool(data.get("hide_prices")),
        subtotal=subtotal, vat_amount=vat, total=total, created_by=user, created_by_name=user.display_name(),
    )
    for l in lines:
        ProformaItem.objects.create(
            proforma=pf, product=l["product"], description=l["product"].name,
            category_name=l["product"].category.name if l["product"].category else "",
            quantity=l["qty"], unit_price=l["unit_price"], list_price=l["list_price"],
            discount=l["discount"], amount=l["amount"],
        )
    for s in sourced:
        ProformaItem.objects.create(
            proforma=pf, product=None, description=s["name"], quantity=s["qty"], unit_price=s["price"],
            list_price=s["price"], amount=s["amount"], is_sourced=True, neighbor_name=s["neighbor"], unit_cost=s["cost"],
        )
    DiscountRequest.objects.filter(pk__in=used_requests).update(consumed=True)
    log(user, "unpaid invoice created", pf, f"{money(total)} FCFA for {c_name}")
    return pf


@transaction.atomic
def cancel_proforma(user, proforma):
    """Withdraws an unpaid invoice that will not be paid. Nothing else changes: it never touched stock."""
    proforma = Proforma.objects.select_for_update().get(pk=proforma.pk)
    if proforma.status != Proforma.OPEN:
        raise SaleError(f"{proforma.number} is already {proforma.get_status_display().lower()}.")
    proforma.status = Proforma.CANCELLED
    proforma.save(update_fields=["status"])
    log(user, "unpaid invoice cancelled", proforma, f"{money(proforma.total)} FCFA")
    return proforma
