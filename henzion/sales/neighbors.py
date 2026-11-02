"""Numbers about items fetched from neighbors: profit, losses and what the shop still owes them.

They are kept out of the shop's own sales figures on purpose; only their profit (or loss) is
added to the boss's profit.
"""
from .models import Neighbor, Sale, SaleItem


def sourced_lines():
    """Neighbor items on sales that were not cancelled."""
    return SaleItem.objects.filter(is_sourced=True, sale__status=Sale.COMPLETED)


def profit_summary(start, end):
    """Profit for sales dated start..end: own stock, neighbor items, and the two together."""
    rows = SaleItem.objects.filter(
        sale__status=Sale.COMPLETED, sale__date__range=(start, end)
    ).values_list("is_sourced", "amount", "quantity", "unit_cost")

    out = {
        "own_profit": 0, "neighbor_profit": 0, "profit": 0,
        "neighbor_lines": 0, "neighbor_qty": 0, "neighbor_revenue": 0, "neighbor_cost": 0,
        "neighbor_loss": 0, "neighbor_loss_lines": 0,
    }
    for sourced, amount, qty, cost in rows:
        profit = amount - qty * cost
        if not sourced:
            out["own_profit"] += profit
            continue
        out["neighbor_profit"] += profit
        out["neighbor_lines"] += 1
        out["neighbor_qty"] += qty
        out["neighbor_revenue"] += amount
        out["neighbor_cost"] += qty * cost
        if profit < 0:
            out["neighbor_loss"] += -profit
            out["neighbor_loss_lines"] += 1
    out["profit"] = out["own_profit"] + out["neighbor_profit"]
    return out


def neighbor_breakdown(start, end):
    """Per neighbor for the period: how much we sold of their items, what it cost, the profit."""
    rows = sourced_lines().filter(sale__date__range=(start, end)).values_list(
        "neighbor__name", "amount", "quantity", "unit_cost"
    )
    per = {}
    for name, amount, qty, cost in rows:
        entry = per.setdefault(name, {"name": name, "items": 0, "revenue": 0, "cost": 0, "profit": 0})
        entry["items"] += qty
        entry["revenue"] += amount
        entry["cost"] += qty * cost
        entry["profit"] += amount - qty * cost
    return sorted(per.values(), key=lambda e: e["name"].lower())


def owed_by_neighbor():
    """What the shop still owes each neighbor for items already sold (all time, until marked paid)."""
    rows = sourced_lines().filter(neighbor_paid_on__isnull=True).values_list(
        "neighbor_id", "neighbor__name", "quantity", "unit_cost"
    )
    per = {}
    for nid, name, qty, cost in rows:
        entry = per.setdefault(nid, {"id": nid, "name": name, "owed": 0, "items": 0})
        entry["owed"] += qty * cost
        entry["items"] += 1
    phones = dict(Neighbor.objects.filter(pk__in=list(per)).values_list("pk", "phone"))
    for entry in per.values():
        entry["phone"] = phones.get(entry["id"], "")
    result = sorted(per.values(), key=lambda e: -e["owed"])
    return {"rows": result, "total": sum(e["owed"] for e in result)}
