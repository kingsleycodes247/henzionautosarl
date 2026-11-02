"""Outgoing notifications: email and WhatsApp (Meta Cloud API).

Without credentials in .env, messages are printed to the terminal so the whole
flow can be tested locally.
"""
import json
import logging
import re
import threading
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def normalize_phone(number):
    """'679 910 139' -> '237679910139' (Cameroon numbers get the 237 prefix)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 9 and digits.startswith("6"):
        digits = "237" + digits
    return digits


def send_email(subject, text, to, html=None, attachments=None):
    if not to:
        return False
    recipients = [to] if isinstance(to, str) else list(to)
    try:
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, recipients)
        if html:
            msg.attach_alternative(html, "text/html")
        for name, content, mimetype in attachments or []:
            msg.attach(name, content, mimetype)
        msg.send()
        return True
    except Exception:  # never break a sale because email failed
        logger.exception("Email sending failed")
        return False


def send_whatsapp(to_number, text):
    to = normalize_phone(to_number)
    if not to:
        return False
    if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_ID):
        print(f"\n[WhatsApp -> +{to}] (not configured, printed instead)\n{text}\n")
        return True

    if settings.WHATSAPP_TEMPLATE_NAME:
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": settings.WHATSAPP_TEMPLATE_NAME,
                "language": {"code": settings.WHATSAPP_TEMPLATE_LANG},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": text.replace("\n", " | ")}]}
                ],
            },
        }
    else:
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}

    req = urllib.request.Request(
        f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_ID}/messages",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError):
        logger.exception("WhatsApp sending failed")
        return False


def run_in_background(func, *args, **kwargs):
    """Send notifications without making the cashier wait."""
    thread = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread
