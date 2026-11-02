from .models import Product


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
        qty = max(p.stock_qty, 0)  # never count negative stock as value
        cost_value = qty * p.cost_price
        retail_value = qty * p.selling_price
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
