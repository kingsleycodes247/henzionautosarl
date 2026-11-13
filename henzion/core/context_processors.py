from django.conf import settings
from django.utils import timezone


def asset_version():
    """Changes whenever a front-end file is edited: used in script URLs and as the service worker's cache name."""
    newest = 0
    for rel in ("js/pos.js", "js/pwa.js", "css/tailwind.css"):
        f = settings.BASE_DIR / "static" / rel
        if f.exists():
            newest = max(newest, int(f.stat().st_mtime))
    return newest


def company(request):
    from .models import CompanySettings

    # Until `npm run build:css` has produced static/css/tailwind.css, the pages fall back to the Tailwind CDN.
    built = (settings.BASE_DIR / "static" / "css" / "tailwind.css").exists()
    # Changes whenever pos.js is edited, so browsers never keep running an old copy of the till script.
    js = settings.BASE_DIR / "static" / "js" / "pos.js"
    version = int(js.stat().st_mtime) if js.exists() else 0
    ctx = {
        "company": CompanySettings.get(), "tailwind_cdn": not built, "pos_js_version": version,
        "asset_version": asset_version(),
        "push_available": bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY),
    }
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from sales.models import Proforma

        ctx["open_proformas"] = Proforma.objects.filter(status=Proforma.OPEN, valid_until__gte=timezone.localdate()).count()
    if user is not None and user.is_authenticated and user.is_owner:
        from sales.models import DiscountRequest

        ctx["pending_discounts"] = DiscountRequest.objects.filter(
            status=DiscountRequest.PENDING, expires_at__gt=timezone.now()
        ).count()
    return ctx
