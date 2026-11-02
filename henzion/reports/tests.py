from datetime import date, timedelta

from django.core import mail
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import CompanySettings
from inventory.models import Product, StockMovement
from reports.models import ReportLog
from reports.services import build_report, period_for, send_report
from sales import services

User = get_user_model()


class PeriodTests(TestCase):
    def test_daily_is_yesterday(self):
        self.assertEqual(period_for("daily", date(2026, 10, 29)), (date(2026, 10, 28), date(2026, 10, 28)))

    def test_weekly_is_last_full_monday_to_sunday(self):
        # Thu 29 Oct 2026 -> the week Mon 19 Oct .. Sun 25 Oct
        self.assertEqual(period_for("weekly", date(2026, 10, 29)), (date(2026, 10, 19), date(2026, 10, 25)))

    def test_monthly_is_last_calendar_month(self):
        self.assertEqual(period_for("monthly", date(2026, 10, 29)), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(period_for("monthly", date(2026, 1, 5)), (date(2025, 12, 1), date(2025, 12, 31)))


class ReportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.product = Product.objects.create(name="Oil", sku="OIL", cost_price=3000, selling_price=5000)
        StockMovement.objects.create(product=self.product, kind="opening", quantity=50)
        cs = CompanySettings.get()
        cs.boss_email = "boss@example.com"
        cs.save()
        self.today = timezone.localdate()

    def sell(self, method, qty=1, **extra):
        data = {"payment_method": method, "items": [{"product_id": self.product.pk, "quantity": qty, "price": 5000}]}
        data.update(extra)
        return services.create_sale(self.owner, data)

    def test_totals_by_method_and_money_received(self):
        self.sell("cash", 2)
        self.sell("mtn_momo", 1)
        # a 15 000 sale split as 5 000 cash + 10 000 on credit
        self.sell("credit", 3, customer={"name": "Jean", "phone": "677111222", "address": "Douala"},
                  due_date=(self.today + timedelta(days=7)).isoformat(),
                  payments=[{"method": "cash", "amount": 5000}, {"method": "credit", "amount": 10000}])
        r = build_report(self.today, self.today)
        methods = {m["key"]: m["total"] for m in r["by_method"]}
        self.assertEqual(methods, {"cash": 15000, "mtn_momo": 5000, "orange_money": 0, "credit": 10000})
        self.assertEqual(r["total_sales"], 30000)
        self.assertEqual(r["credit_issued"], 10000)
        self.assertEqual(r["debt_collected"], 0)  # nobody has paid a debt yet
        self.assertEqual(r["total_received"], 20000)  # cash 15 000 + momo 5 000
        self.assertEqual(r["profit"], 6 * 2000)

    def test_email_is_sent_once_with_csv(self):
        self.sell("cash")
        start = end = self.today
        ok, _ = send_report("daily", start, end)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["boss@example.com"])
        self.assertTrue(mail.outbox[0].attachments[0][0].endswith(".csv"))

        ok, message = send_report("daily", start, end)  # a second cron run must not email again
        self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ReportLog.objects.filter(ok=True).count(), 1)


class NeighborReportTests(TestCase):
    """Neighbor items stay out of sales numbers; only their profit joins the boss's profit."""

    setUp = ReportTests.setUp
    sell = ReportTests.sell

    def sell_with_neighbor(self):
        return self.sell("cash", 2, sourced_items=[{"name": "Brake disc", "neighbor": "Mama Ngu", "quantity": 1, "cost": 1200, "price": 2000}])

    def test_sales_exclude_neighbor_items_but_profit_includes_them(self):
        self.sell_with_neighbor()
        r = build_report(self.today, self.today)
        self.assertEqual(r["total_sales"], 10000)
        self.assertEqual(r["sales_count"], 1)
        self.assertEqual(r["neighbor_money"], 2000)
        self.assertEqual(r["own_profit"], 4000)
        self.assertEqual(r["profit"], 4800)
        self.assertEqual(r["neighbor"]["neighbor_profit"], 800)
        self.assertEqual(r["neighbor_breakdown"][0]["name"], "Mama Ngu")
        self.assertEqual(r["neighbor_owed"]["total"], 1200)
        self.assertEqual([p["description"] for p in r["top_products"]], [self.product.name])
        self.assertEqual(r["daily"][0]["total"], 10000)

    def test_till_money_still_includes_neighbor_money(self):
        self.sell_with_neighbor()
        r = build_report(self.today, self.today)
        self.assertEqual(r["total_received"], 12000)

    def test_csv_marks_neighbor_lines(self):
        from reports.services import sales_csv

        self.sell_with_neighbor()
        csv_text = sales_csv(self.today, self.today)
        self.assertIn("Neighbor item", csv_text)
        self.assertIn("Mama Ngu", csv_text)
        self.assertIn("Own stock", csv_text)

    def test_email_mentions_neighbor_items(self):
        self.sell_with_neighbor()
        ok, _ = send_report("daily", self.today, self.today)
        self.assertTrue(ok)
        self.assertIn("Neighbor items", mail.outbox[0].body)
        self.assertIn("Mama Ngu", mail.outbox[0].body)


class EndOfDayTests(TestCase):
    """The evening report: dashboard button, and the automatic send when a cashier logs out."""

    def setUp(self):
        from datetime import datetime, time
        from unittest import mock

        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("cash", password="x", role=User.CASHIER)
        self.product = Product.objects.create(name="Engine oil", sku="OIL", cost_price=3000, selling_price=5000)
        StockMovement.objects.create(product=self.product, kind="opening", quantity=50)
        self.cs = CompanySettings.get()
        self.cs.boss_email = "boss@example.com"
        self.cs.eod_start_hour, self.cs.eod_end_hour, self.cs.eod_auto_on_logout = 18, 20, True
        self.cs.save()
        self.today = timezone.localdate()
        at = lambda h: timezone.make_aware(datetime.combine(self.today, time(h, 0)))
        self.morning, self.evening, self.late = at(10), at(19), at(21)
        patcher = mock.patch("sales.services.notify.run_in_background", side_effect=lambda f, *a, **k: f(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sell(self, price=5000, qty=2, **extra):
        data = {"payment_method": "cash", "items": [{"product_id": self.product.pk, "quantity": qty, "price": price}]}
        data.update(extra)
        return services.create_sale(self.cashier, data)

    def test_banner_needs_the_start_hour_and_at_least_one_sale(self):
        from reports.services import eod_state

        self.assertFalse(eod_state(self.evening)["show_banner"])  # nothing sold yet
        self.sell()
        self.assertFalse(eod_state(self.morning)["show_banner"])  # too early
        self.assertTrue(eod_state(self.evening)["show_banner"])
        self.assertTrue(eod_state(self.late)["show_banner"])  # still offered after the window, until midnight
        self.assertFalse(eod_state(self.late)["in_window"])

    def test_report_contents_items_profit_methods_and_credit(self):
        from reports.services import send_eod_report

        self.sell(qty=2)
        self.sell(qty=1, payment_method="mtn_momo")
        self.sell(qty=3, payment_method="credit", customer={"name": "Jean Mbarga", "phone": "677111222", "address": "Douala"},
                  due_date=(self.today + timedelta(days=7)).isoformat())
        r = build_report(self.today, self.today)
        self.assertEqual(r["items_sold"][0]["qty"], 6)
        self.assertEqual(r["credit_sales"][0]["customer"], "Jean Mbarga")
        self.assertEqual(r["credit_sales"][0]["amount"], 15000)
        self.assertEqual(r["profit"], 6 * 2000)

        ok, _ = send_eod_report()
        self.assertTrue(ok)
        body = mail.outbox[0].body
        self.assertIn("End-of-day", mail.outbox[0].subject)
        self.assertIn("Engine oil", body)
        self.assertIn("Jean Mbarga", body)
        self.assertIn("Credit sales", mail.outbox[0].alternatives[0][0])

    def test_banner_goes_away_after_sending_and_returns_for_new_sales(self):
        from reports.services import eod_state, send_eod_report

        self.sell()
        send_eod_report()
        state = eod_state(self.evening)
        self.assertTrue(state["sent_today"])
        self.assertFalse(state["show_banner"])
        self.sell()
        state = eod_state(self.evening)
        self.assertEqual(state["new_sales"], 1)
        self.assertTrue(state["show_banner"])

    def test_cashier_logout_in_the_window_sends_once(self):
        from reports.services import eod_on_logout

        self.sell()
        eod_on_logout(self.cashier, self.evening)
        self.assertEqual(len(mail.outbox), 1)
        eod_on_logout(self.cashier, self.evening)  # nothing new to report
        self.assertEqual(len(mail.outbox), 1)
        self.sell()
        eod_on_logout(self.cashier, self.evening)  # a new sale since
        self.assertEqual(len(mail.outbox), 2)

    def test_logout_sends_nothing_outside_the_window_for_the_boss_or_when_switched_off(self):
        from reports.services import eod_on_logout

        self.sell()
        eod_on_logout(self.cashier, self.morning)
        eod_on_logout(self.cashier, self.late)
        eod_on_logout(self.owner, self.evening)
        eod_on_logout(None, self.evening)
        self.assertEqual(len(mail.outbox), 0)

        self.cs.eod_auto_on_logout = False
        self.cs.save()
        eod_on_logout(self.cashier, self.evening)
        self.assertEqual(len(mail.outbox), 0)
        self.cs.eod_auto_on_logout, self.cs.boss_email = True, ""
        self.cs.save()
        eod_on_logout(self.cashier, self.evening)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failing_email_never_breaks_logout(self):
        from unittest import mock
        from reports.services import eod_on_logout

        self.sell()
        with mock.patch("reports.services.send_eod_report", side_effect=RuntimeError("smtp down")):
            eod_on_logout(self.cashier, self.evening)  # must not raise

    def test_real_logout_calls_the_hook(self):
        from unittest import mock

        with mock.patch("reports.signals.eod_on_logout") as hook:
            self.client.force_login(self.cashier)
            self.client.post("/accounts/logout/")
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[0], self.cashier)

    def test_dashboard_button_sends_the_report_and_is_owner_only(self):
        from django.urls import reverse

        self.cs.eod_start_hour = 0
        self.cs.save()
        self.sell()
        self.client.force_login(self.owner)
        res = self.client.get(reverse("dashboard"))
        self.assertContains(res, "Send to my email")
        res = self.client.post(reverse("eod_send"))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotContains(self.client.get(reverse("dashboard")), "Send to my email")

        self.client.force_login(self.cashier)
        self.assertEqual(self.client.post(reverse("eod_send")).status_code, 403)

    def test_settings_window_must_end_after_it_starts(self):
        from core.forms import CompanySettingsForm

        form = CompanySettingsForm(instance=self.cs, data={
            "name": "X", "tagline": "t", "description": "d", "address": "a", "phones": "p", "niu": "n",
            "vat_rate": 0, "auto_approve_percent": 0, "approval_timeout_minutes": 10,
            "eod_start_hour": 20, "eod_end_hour": 18, "eod_auto_on_logout": "on",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("eod_end_hour", form.errors)
