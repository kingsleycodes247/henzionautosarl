"""Web Push (notifications to the phone/laptop even when the app is closed).

Needs VAPID keys in .env (`python manage.py generate_vapid_keys`) and the `pywebpush` package.
When either is missing every function here quietly does nothing, so the rest of the app is unaffected.
"""
import json
import logging

from django.conf import settings

from . import notify

log = logging.getLogger(__name__)


def is_configured():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def _targets(users):
    from .models import PushSubscription

    return list(
        PushSubscription.objects.filter(user__in=users, user__is_active=True)
        .values("pk", "endpoint", "p256dh", "auth")
    )


def _send_all(targets, payload):
    """Runs in a background thread (no request, no slow work in the till)."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush is not installed: push notification skipped.")
        return
    dead = []
    for t in targets:
        try:
            webpush(
                subscription_info={"endpoint": t["endpoint"], "keys": {"p256dh": t["p256dh"], "auth": t["auth"]}},
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_CONTACT},
                ttl=3600,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):  # the phone unsubscribed or reinstalled: forget it
                dead.append(t["pk"])
            else:
                log.warning("Push failed: %s", exc)
        except Exception as exc:  # never let a notification problem break anything
            log.warning("Push failed: %s", exc)
    if dead:
        from .models import PushSubscription

        PushSubscription.objects.filter(pk__in=dead).delete()


def notify_users(users, title, body, url="/", tag=None):
    """Queue a notification for these users' devices. Safe to call anywhere; returns the device count."""
    if not is_configured():
        return 0
    targets = _targets(users)
    if not targets:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or "henzion"})
    notify.run_in_background(_send_all, targets, payload)
    return len(targets)


def owners():
    from django.db.models import Q

    from .models import User

    return User.objects.filter(is_active=True).filter(Q(role=User.OWNER) | Q(is_superuser=True))
