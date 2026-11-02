from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from debts.models import Customer, Debt, DebtPayment

User = get_user_model()


class DebtRegisterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("cash", password="x", role=User.CASHIER)
        self.customer = Customer.objects.create(name="Amadou Garage", phone="677000111", address="Bonaberi")
        self.today = timezone.localdate()

    def make_debt(self, total=50000, due_in=10):
        return Debt.objects.create(
            customer=self.customer, description="Tyres", total=total,
            issued_on=self.today, due_date=self.today + timedelta(days=due_in),
        )

    def pay_data(self, amount, **extra):
        data = {"amount": amount, "method": "cash", "reference": "", "paid_on": self.today.isoformat(), "note": ""}
        data.update(extra)
        return data

    # --- the boss can do full CRUD ---------------------------------------------------------
    def test_owner_full_crud_on_debts_and_payments(self):
        self.client.force_login(self.owner)

        # create
        res = self.client.post(reverse("debt_create"), {
            "customer": self.customer.pk, "description": "Brake pads", "total": 50000,
            "issued_on": self.today.isoformat(), "due_date": (self.today + timedelta(days=7)).isoformat(), "notes": "",
        })
        debt = Debt.objects.get()
        self.assertRedirects(res, reverse("debt_detail", args=[debt.pk]))

        # read
        self.assertEqual(self.client.get(reverse("debt_list")).status_code, 200)
        self.assertContains(self.client.get(reverse("debt_detail", args=[debt.pk])), "Brake pads")

        # update
        self.client.post(reverse("debt_update", args=[debt.pk]), {
            "customer": self.customer.pk, "description": "Brake pads x2", "total": 60000,
            "issued_on": self.today.isoformat(), "due_date": "", "notes": "",
        })
        debt.refresh_from_db()
        self.assertEqual((debt.description, debt.total), ("Brake pads x2", 60000))

        # payments: add, edit, delete
        self.client.post(reverse("payment_create", args=[debt.pk]), self.pay_data(20000))
        payment = DebtPayment.objects.get()
        self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).balance_amount, 40000)

        self.client.post(reverse("payment_update", args=[payment.pk]), self.pay_data(60000))
        self.assertTrue(Debt.objects.with_balance().get(pk=debt.pk).is_paid)

        self.client.post(reverse("payment_delete", args=[payment.pk]))
        self.assertEqual(DebtPayment.objects.count(), 0)
        self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).balance_amount, 60000)

        # delete
        self.client.post(reverse("debt_delete", args=[debt.pk]))
        self.assertEqual(Debt.objects.count(), 0)

    def test_owner_crud_on_customers(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("customer_create"), {"name": "Paul", "phone": "699111000", "credit_limit": 0})
        self.assertTrue(Customer.objects.filter(name="Paul").exists())
        paul = Customer.objects.get(name="Paul")
        self.client.post(reverse("customer_update", args=[paul.pk]), {"name": "Paul N.", "phone": "699111000", "credit_limit": 100000})
        paul.refresh_from_db()
        self.assertEqual((paul.name, paul.credit_limit), ("Paul N.", 100000))
        self.client.post(reverse("customer_delete", args=[paul.pk]))
        self.assertFalse(Customer.objects.filter(pk=paul.pk).exists())

    # --- payment rules ------------------------------------------------------------------------
    def test_cannot_overpay(self):
        debt = self.make_debt()
        self.client.force_login(self.owner)
        res = self.client.post(reverse("payment_create", args=[debt.pk]), self.pay_data(60000))
        self.assertEqual(res.status_code, 200)  # form re-shown with an error
        self.assertEqual(DebtPayment.objects.count(), 0)

    def test_partial_payments_until_settled(self):
        debt = self.make_debt(total=30000)
        self.client.force_login(self.cashier)
        for amount, expected_balance in ((10000, 20000), (5000, 15000), (15000, 0)):
            self.client.post(reverse("payment_create", args=[debt.pk]), self.pay_data(amount))
            self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).balance_amount, expected_balance)
        self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).status, "paid")

    def test_cannot_lower_total_below_amount_already_paid(self):
        debt = self.make_debt()
        DebtPayment.objects.create(debt=debt, amount=30000, method="cash")
        self.client.force_login(self.owner)
        self.client.post(reverse("debt_update", args=[debt.pk]), {
            "customer": self.customer.pk, "description": "Tyres", "total": 10000,
            "issued_on": self.today.isoformat(), "due_date": "", "notes": "",
        })
        debt.refresh_from_db()
        self.assertEqual(debt.total, 50000)

    def test_overdue_status(self):
        debt = self.make_debt(due_in=-3)
        self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).status, "overdue")

    # --- cashier limits -----------------------------------------------------------------------
    def test_cashier_cannot_edit_or_delete(self):
        debt = self.make_debt()
        payment = DebtPayment.objects.create(debt=debt, amount=1000, method="cash")
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("debt_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("debt_delete", args=[debt.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("payment_delete", args=[payment.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("customer_create")).status_code, 403)
        self.assertEqual(Debt.objects.count(), 1)

    def test_login_required(self):
        res = self.client.get(reverse("debt_list"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    # --- dashboards ---------------------------------------------------------------------------
    def test_owner_dashboard_shows_the_debts_panel(self):
        self.make_debt(due_in=-5)
        self.client.force_login(self.owner)
        res = self.client.get(reverse("dashboard"))
        self.assertContains(res, "Debts register")
        self.assertContains(res, "Amadou Garage")
        self.assertContains(res, "50 000")

    def test_cashier_dashboard_has_no_owner_panel(self):
        self.make_debt()
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, "Biggest debtors")

    def test_receipt_and_other_pages_render(self):
        debt = self.make_debt()
        payment = DebtPayment.objects.create(debt=debt, amount=10000, method="mtn_momo", reference="MP123")
        self.client.force_login(self.owner)
        for name, args in [
            ("payment_receipt", [payment.pk]), ("customer_list", []), ("customer_detail", [self.customer.pk]),
            ("payment_update", [payment.pk]), ("debt_update", [debt.pk]), ("debt_delete", [debt.pk]),
            ("product_list", []), ("movement_list", []), ("sale_list", []), ("discount_list", []),
            ("neighbor_items", []), ("reports", []), ("user_list", []), ("audit_log", []), ("company_settings", []),
        ]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
