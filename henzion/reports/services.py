import csv
import io
from datetime import date, timedelta

from django.db.models import Count, F, Sum
from django.template.loader import render_to_string
from django.utils import timezone

from core import notify
from core.constants import CREDIT, METHOD_LABELS, PAYMENT_METHODS, SALE_METHODS
from core.models import CompanySettings
from debts.models import Debt, DebtPayment
from debts.services import debt_summary
from inventory.models import Product
from sales.models import DiscountRequest, Sale, SaleItem, SalePayment
from sales.neighbors import neighbor_breakdown, owed_by_neighbor, profit_summary

from .models import ReportLog


def period_for(kind, ref=None):
    """The period a scheduled report covers, relative to `ref` (default: today).

    daily   -> yesterday
    weekly  -> the last full Monday-Sunday week
    monthly -> the last full calendar month
    """
    ref = ref or timezone.localdate()
    if kind == ReportLog.DAILY:
        day = ref - timedelta(days=1)
        return day, day
    if kind == ReportLog.WEEKLY:
        this_monday = ref - timedelta(days=ref.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if kind == ReportLog.MONTHLY:
        first_this = ref.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    raise ValueError(f"Unknown report kind: {kind}")


def build_report(start, end):
    sales = Sale.objects.filter(status=Sale.COMPLETED, date__range=(start, end))
    items = SaleItem.objects.filter(sale__in=sales, is_sourced=False)  # our own stock only

    # Per method, from the payment lines: a split sale counts under every method it used.
    by_method = []
    for key, label in SALE_METHODS:
        agg = SalePayment.objects.filter(sale__in=sales, method=key).aggregate(n=Count("sale", distinct=True), total=Sum("amount"))
        by_method.append({"key": key, "label": label, "count": agg["n"] or 0, "total": agg["total"] or 0})

    # Sales = our own stock. Neighbor items are left out; only their profit is counted (see `neighbor`).
    sales_agg = sales.aggregate(own=Sum(F("total") - F("sourced_total")), neighbor=Sum("sourced_total"))
    total_sales = sales_agg["own"] or 0
    neighbor_money = sales_agg["neighbor"] or 0  # is inside the per-method money below
    sales_count = sales.filter(total__gt=F("sourced_total")).count()
    credit_issued = next(m["total"] for m in by_method if m["key"] == CREDIT)

    # Money that actually came in: paid sales + payments on debts (including deposits).
    payments = DebtPayment.objects.filter(paid_on__range=(start, end))
    received = []
    for key, label in PAYMENT_METHODS:
        sold = next(m["total"] for m in by_method if m["key"] == key)
        collected = payments.filter(method=key).aggregate(s=Sum("amount"))["s"] or 0
        received.append({"label": label, "sales": sold, "debt_payments": collected, "total": sold + collected})
    debt_collected = sum(r["debt_payments"] for r in received)

    top_products = list(
        items.values("description").annotate(qty=Sum("quantity"), amount=Sum("amount")).order_by("-amount")[:10]
    )
    psum = profit_summary(start, end)
    discount_given = items.aggregate(s=Sum("discount"))["s"] or 0

    items_sold = list(
        items.values("description").annotate(qty=Sum("quantity"), amount=Sum("amount")).order_by("-amount")[:100]
    )
    credit_sales = [
        {"customer": d.customer.name, "phone": d.customer.phone, "invoice": d.sale.number if d.sale else d.description,
         "amount": d.total, "due_date": d.due_date}
        for d in Debt.objects.filter(sale__in=sales).select_related("customer", "sale").order_by("-total")
    ]

    requests = DiscountRequest.objects.filter(created_at__date__range=(start, end))
    daily = []
    day = start
    while day <= end and (end - start).days <= 62:
        agg = sales.filter(date=day, total__gt=F("sourced_total")).aggregate(n=Count("id"), total=Sum(F("total") - F("sourced_total")))
        daily.append({"date": day, "count": agg["n"] or 0, "total": agg["total"] or 0})
        day += timedelta(days=1)

    low_stock = [p for p in Product.objects.with_stock().filter(is_active=True) if p.stock_qty <= p.reorder_level][:15]

    return {
        "start": start, "end": end,
        "sales_count": sales_count, "total_sales": total_sales, "neighbor_money": neighbor_money,
        "by_method": by_method, "credit_issued": credit_issued,
        "received": received, "total_received": sum(r["total"] for r in received),
        "debt_collected": debt_collected,
        "profit": psum["profit"], "own_profit": psum["own_profit"], "neighbor": psum,
        "neighbor_breakdown": neighbor_breakdown(start, end), "neighbor_owed": owed_by_neighbor(),
        "discount_given": discount_given,
        "discounts_approved": requests.filter(status=DiscountRequest.APPROVED).count(),
        "discounts_declined": requests.filter(status__in=[DiscountRequest.DECLINED, DiscountRequest.EXPIRED]).count(),
        "top_products": top_products, "items_sold": items_sold, "credit_sales": credit_sales, "daily": daily,
        "generated_at": timezone.localtime(),
        "low_stock": low_stock,
        "debts": debt_summary(),
    }


def sales_csv(start, end):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Invoice", "Date", "Customer", "Payment method", "Product", "Qty", "Unit price", "Discount", "Amount", "Type", "Neighbor", "Unit cost"])
    rows = SaleItem.objects.filter(
        sale__status=Sale.COMPLETED, sale__date__range=(start, end)
    ).select_related("sale", "neighbor").order_by("sale__date", "sale_id")
    for i in rows:
        writer.writerow([
            i.sale.number, i.sale.date, i.sale.customer_name or "Walk-in",
            METHOD_LABELS[i.sale.payment_method], i.description, i.quantity, i.unit_price, i.discount, i.amount,
            "Neighbor item" if i.is_sourced else "Own stock",
            i.neighbor.name if i.is_sourced else "", i.unit_cost if i.is_sourced else "",
        ])
    return buf.getvalue()


def report_title(kind, start, end):
    names = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "eod": "End-of-day"}
    span = f"{start:%d %b %Y}" if start == end else f"{start:%d %b} - {end:%d %b %Y}"
    if kind == "eod":
        return f"End-of-day sales report: {span}, as of {timezone.localtime():%H:%M}"
    return f"{names.get(kind, '')} sales report: {span}"


def send_report(kind, start, end, force=False):
    """Emails a report to the boss. Returns (ok, message)."""
    cs = CompanySettings.get()
    if not cs.boss_email:
        return False, "No boss email is set in Settings."
    if not force and ReportLog.objects.filter(kind=kind, period_start=start, ok=True).exists():
        return False, "Already sent."

    data = build_report(start, end)
    title = report_title(kind, start, end)
    ctx = {"r": data, "title": title, "company": cs, "kind": kind}
    html = render_to_string("reports/email_report.html", ctx)
    text = render_to_string("reports/email_report.txt", ctx)
    ok = notify.send_email(
        f"{cs.name} - {title}", text, cs.boss_email, html=html,
        attachments=[(f"sales_{start}_{end}.csv", sales_csv(start, end), "text/csv")],
    )
    ReportLog.objects.create(
        kind=kind, period_start=start, period_end=end, recipient=cs.boss_email, ok=ok,
        error="" if ok else "Email could not be sent; see server log.",
    )
    return ok, "Sent." if ok else "Email failed; check the server log."


# --------------------------------------------------------------------------- end of day

def eod_state(now=None):
    """Where today's end-of-day report stands, for the dashboard banner and the logout trigger.

    The banner shows from the start hour until midnight when today has sales that no emailed
    end-of-day report covers yet. A sale made after a report was sent makes it show again.
    """
    cs = CompanySettings.get()
    local = timezone.localtime(now or timezone.now())
    today = local.date()
    todays = Sale.objects.filter(status=Sale.COMPLETED, date=today)
    last = ReportLog.objects.filter(kind=ReportLog.EOD, period_start=today, ok=True).first()
    new_sales = todays.filter(created_at__gt=last.sent_at).count() if last else todays.count()
    return {
        "after_start": local.hour >= cs.eod_start_hour,
        "in_window": cs.eod_start_hour <= local.hour < cs.eod_end_hour,
        "sales_today": todays.count(),
        "sent_today": last is not None,
        "last_sent": timezone.localtime(last.sent_at) if last else None,
        "new_sales": new_sales,
        "show_banner": local.hour >= cs.eod_start_hour and new_sales > 0,
        "boss_email": cs.boss_email,
    }


def send_eod_report():
    """Emails today's report to the boss right now. Returns (ok, message)."""
    today = timezone.localdate()
    return send_report(ReportLog.EOD, today, today, force=True)


def eod_on_logout(user, now=None):
    """Called when someone logs out: a cashier leaving inside the evening window sends the report,
    unless an end-of-day report already covers all of today's sales. Never raises."""
    try:
        if user is None or user.is_owner:
            return
        cs = CompanySettings.get()
        if not (cs.eod_auto_on_logout and cs.boss_email):
            return
        state = eod_state(now)
        if state["in_window"] and state["show_banner"]:
            send_eod_report()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Automatic end-of-day report failed")
