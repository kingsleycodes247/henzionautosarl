"""Quantities can be whole or a half (x.5, for cartons sold by the half). Money is whole FCFA."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ONE = Decimal("1")
TENTH = Decimal("0.1")


def line_money(price, qty):
    """price x quantity as whole FCFA (a half of an odd price rounds up)."""
    return int((Decimal(price) * Decimal(qty)).quantize(ONE, rounding=ROUND_HALF_UP))


def fmt_qty(value):
    """4.0 -> '4', 4.5 -> '4.5'"""
    try:
        q = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    return str(int(q)) if q == q.to_integral_value() else str(q.quantize(TENTH))


def num_qty(value):
    """Same idea but returns a number: int when whole, otherwise a float (for JSON)."""
    q = Decimal(value or 0)
    return int(q) if q == q.to_integral_value() else float(q.quantize(TENTH))
