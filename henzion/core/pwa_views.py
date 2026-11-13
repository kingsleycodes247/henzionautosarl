import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET, require_POST

from . import push
from .context_processors import asset_version
from .models import CompanySettings, PushSubscription


@require_GET
def manifest(request):
    company = CompanySettings.get()
    data = {
        "name": company.name,
        "short_name": "HenZion",
        "description": "Inventory, sales and debts",
        "id": "/",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#1e293b",
        "theme_color": "#1e293b",
        "icons": [
            {"src": static("img/pwa/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("img/pwa/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("img/pwa/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "New sale", "url": "/sales/new/", "icons": [{"src": static("img/pwa/icon-192.png"), "sizes": "192x192"}]},
        ],
    }
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


@require_GET
def service_worker(request):
    """Served from the site root so it can control every page. Never cached by the browser itself."""
    resp = render(request, "pwa/sw.js", {
        "cache_version": asset_version(),
        "offline_url": "/offline/",
        "icon": static("img/pwa/icon-192.png"),
        "badge": static("img/pwa/badge-96.png"),
        "precache": [static("img/pwa/icon-192.png"), static("img/logo.jpeg")],
    }, content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"
    resp["Service-Worker-Allowed"] = "/"
    return resp


@require_GET
def offline(request):
    return render(request, "pwa/offline.html", {"company": CompanySettings.get()})


@login_required
@require_GET
def push_status(request):
    return JsonResponse({
        "enabled": push.is_configured(),
        "public_key": settings.VAPID_PUBLIC_KEY,
        "devices": request.user.push_subscriptions.count(),
    })


@login_required
@require_POST
def push_subscribe(request):
    if not push.is_configured():
        return JsonResponse({"ok": False, "error": "Notifications are not set up on the server yet."}, status=400)
    try:
        data = json.loads(request.body)
        endpoint = data["endpoint"]
        p256dh = data["keys"]["p256dh"]
        auth = data["keys"]["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid subscription."}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth,
                  "user_agent": request.META.get("HTTP_USER_AGENT", "")[:250]},
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def push_unsubscribe(request):
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False}, status=400)
    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def push_test(request):
    sent = push.notify_users([request.user], "HenZion test", "Notifications work on this device.", url="/", tag="test")
    if not sent:
        return JsonResponse({"ok": False, "error": "No device is registered for your account yet."}, status=400)
    return JsonResponse({"ok": True, "devices": sent})
