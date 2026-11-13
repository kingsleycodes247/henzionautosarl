from django import template

from core.qty import fmt_qty, line_money

register = template.Library()


@register.filter
def fcfa(value):
    """25000 -> '25 000' (XAF has no decimals)."""
    try:
        return f"{int(round(float(value))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


@register.filter
def mul(value, other):
    """{{ qty|mul:cost }}"""
    try:
        return line_money(other, value)
    except (TypeError, ValueError, ArithmeticError):
        return 0


@register.filter
def pct(value, total):
    try:
        return round(float(value) * 100 / float(total)) if float(total) else 0
    except (TypeError, ValueError):
        return 0


@register.simple_tag(takes_context=True)
def query_string(context):
    """Current filters without ?page=, ready to prepend to 'page=N' in pagination links."""
    params = context["request"].GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return encoded + "&" if encoded else ""


@register.simple_tag(takes_context=True)
def nav_active(context, *prefixes):
    path = context["request"].path
    for prefix in prefixes:
        if (prefix == "/" and path == "/") or (prefix != "/" and path.startswith(prefix)):
            return "bg-slate-700 text-white"
    return "text-slate-300 hover:bg-slate-800"


@register.filter
def qty(value):
    """4.0 -> 4, 4.5 -> 4.5"""
    return fmt_qty(value)


@register.filter
def qty_signed(value):
    text = fmt_qty(value)
    return text if text.startswith("-") else f"+{text}"
