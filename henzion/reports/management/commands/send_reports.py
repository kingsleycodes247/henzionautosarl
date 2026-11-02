from datetime import date

from django.core.management.base import BaseCommand, CommandError

from reports.services import period_for, send_report


class Command(BaseCommand):
    help = (
        "Email the daily / weekly / monthly sales report to the boss.\n"
        "  daily   = yesterday, weekly = last Mon-Sun week, monthly = last calendar month.\n"
        "Schedule it with cron (see README)."
    )

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["daily", "weekly", "monthly"])
        parser.add_argument("--date", help="Pretend today is this date (YYYY-MM-DD).")
        parser.add_argument("--force", action="store_true", help="Send even if this period was already sent.")

    def handle(self, *args, **opts):
        ref = None
        if opts["date"]:
            try:
                ref = date.fromisoformat(opts["date"])
            except ValueError:
                raise CommandError("--date must look like 2026-10-29")
        start, end = period_for(opts["kind"], ref)
        ok, message = send_report(opts["kind"], start, end, force=opts["force"])
        self.stdout.write(f"{opts['kind']} report {start} to {end}: {message}")
        if not ok and message not in ("Already sent.",):
            raise CommandError(message)
