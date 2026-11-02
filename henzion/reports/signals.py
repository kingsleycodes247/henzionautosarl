from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver

from .services import eod_on_logout


@receiver(user_logged_out)
def send_report_when_cashier_logs_out(sender, request, user, **kwargs):
    eod_on_logout(user)
