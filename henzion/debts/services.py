from datetime import timedelta

from django.utils import timezone

from .models import Debt, DebtPayment


def debt_summary(today=None):
    """Numbers for the boss's dashboard and the reports."""
    today = today or timezone.localdate()
    open_debts = list(Debt.objects.with_balance().filter(balance_sum__gt=0).select_related("customer"))

    outstanding = sum(d.balance_amount for d in open_debts)
    overdue = sorted(
        (d for d in open_debts if d.due_date and d.due_date < today), key=lambda d: d.due_date
    )
    due_soon = sorted(
        (d for d in open_debts if d.due_date and today <= d.due_date <= today + timedelta(days=7)),
        key=lambda d: d.due_date,
    )

    per_customer = {}
    for d in open_debts:
        entry = per_customer.setdefault(d.customer_id, {"customer": d.customer, "balance": 0, "count": 0})
        entry["balance"] += d.balance_amount
        entry["count"] += 1
    top_debtors = sorted(per_customer.values(), key=lambda e: -e["balance"])[:5]

    # How late the unpaid money is (days past the due date).
    ageing = {"not_due": 0, "late_0_30": 0, "late_31_60": 0, "late_61_plus": 0}
    for d in open_debts:
        if not d.due_date or d.due_date >= today:
            ageing["not_due"] += d.balance_amount
            continue
        late = (today - d.due_date).days
        key = "late_0_30" if late <= 30 else "late_31_60" if late <= 60 else "late_61_plus"
        ageing[key] += d.balance_amount

    return {
        "outstanding": outstanding,
        "open_count": len(open_debts),
        "debtor_count": len(per_customer),
        "overdue": overdue,
        "overdue_total": sum(d.balance_amount for d in overdue),
        "due_soon": due_soon,
        "top_debtors": top_debtors,
        "ageing": ageing,
        "recent_payments": DebtPayment.objects.select_related("debt__customer").order_by("-created_at")[:6],
    }
