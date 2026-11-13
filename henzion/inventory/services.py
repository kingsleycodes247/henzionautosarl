from django.db import transaction
from django.db.models import Sum

from core.models import log
from core.qty import fmt_qty, line_money, num_qty

from .models import Product, StockMovement, clean_qty


def stock_valuation():
    """What the goods on the shelf are worth.

    cost    = quantity x cost price      (what the boss paid / has tied up in stock)
    retail  = quantity x selling price   (what it brings in if it all sells at list price)
    profit  = retail - cost              (potential gross profit)
    """
    rows = []
    categories = {}
    totals = {"units": 0, "cost": 0, "retail": 0, "profit": 0, "products": 0, "missing_cost": 0}

    for p in Product.objects.with_stock().select_related("category").filter(is_active=True):
        qty = clean_qty(max(p.stock_qty, 0))  # never count negative stock as value
        cost_value = line_money(p.cost_price, qty)
        retail_value = line_money(p.selling_price, qty)
        rows.append({
            "product": p, "qty": qty,
            "cost_value": cost_value, "retail_value": retail_value, "profit": retail_value - cost_value,
        })

        name = p.category.name if p.category else "No category"
        cat = categories.setdefault(name, {"name": name, "units": 0, "cost": 0, "retail": 0})
        cat["units"] += qty
        cat["cost"] += cost_value
        cat["retail"] += retail_value

        totals["units"] += qty
        totals["cost"] += cost_value
        totals["retail"] += retail_value
        if qty > 0:
            totals["products"] += 1
            if p.cost_price == 0:
                totals["missing_cost"] += 1  # stock with no cost price makes the cost value too low

    totals["profit"] = totals["retail"] - totals["cost"]
    rows.sort(key=lambda r: -r["cost_value"])
    return {
        "rows": rows,
        "totals": totals,
        "categories": sorted(categories.values(), key=lambda c: -c["cost"]),
    }


class StockError(Exception):
    pass


@transaction.atomic
def open_carton(user, carton_id, count=1):
    """Moves whole cartons from the store onto the shelf: the carton product goes down by `count`,
    its retail listing goes up by count x pieces-per-carton. Both rows are in the stock history."""
    carton = Product.objects.filter(pk=carton_id, is_active=True).first()
    if carton is None or not carton.can_open_carton:
        raise StockError("This product has no retail listing set up for opening cartons.")
    count = int(count)
    if count < 1:
        raise StockError("Open at least one carton.")
    locked = {p.pk: p for p in Product.objects.select_for_update().filter(pk__in=[carton.pk, carton.retail_version_id]).order_by("pk")}
    carton, retail = locked[carton.pk], locked.get(carton.retail_version_id)
    if retail is None:
        raise StockError("The retail listing of this carton no longer exists.")
    in_store = StockMovement.objects.filter(product=carton).aggregate(s=Sum("quantity"))["s"] or 0
    if in_store < count:
        left = fmt_qty(in_store)
        raise StockError(f"Only {left} carton(s) of {carton.name} left in the store, so {count} can't be opened.")
    pieces = count * carton.pieces_per_carton
    note = f"Opened {count} carton(s) = {pieces} pieces moved to the shelf"
    StockMovement.objects.create(product=carton, kind=StockMovement.TRANSFER, quantity=-count, note=note, created_by=user)
    StockMovement.objects.create(product=retail, kind=StockMovement.TRANSFER, quantity=pieces, note=note, created_by=user)
    log(user, "carton opened", carton, f"{count} -> +{pieces} {retail.name}")
    return {
        "pieces": pieces,
        "carton_id": carton.pk, "carton_stock": num_qty(in_store - count),
        "retail_id": retail.pk, "retail_stock": num_qty(
            StockMovement.objects.filter(product=retail).aggregate(s=Sum("quantity"))["s"] or 0),
    }
