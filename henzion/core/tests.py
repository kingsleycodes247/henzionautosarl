from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest import mock

from core.models import AuditLog
from debts.models import Customer, Debt, DebtPayment
from inventory.models import Product, StockMovement
from sales import services
from sales.models import Sale

User = get_user_model()


class UserRemovalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("marie", password="x", role=User.CASHIER, first_name="Marie", last_name="Ngo")
        product = Product.objects.create(name="Oil", sku="OIL", cost_price=3000, selling_price=5000)
        StockMovement.objects.create(product=product, kind="opening", quantity=20)
        patcher = mock.patch("sales.services.notify.run_in_background", side_effect=lambda f, *a, **k: f(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sale = services.create_sale(self.cashier, {"payment_method": "cash", "items": [{"product_id": product.pk, "quantity": 1, "price": 5000}]})
        self.client.force_login(self.owner)

    def test_sale_stores_the_cashiers_name(self):
        self.assertEqual(self.sale.created_by_name, "Marie Ngo")

    def test_deleting_a_user_keeps_sales_invoices_and_the_name(self):
        res = self.client.post(reverse("user_delete", args=[self.cashier.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.cashier.pk).exists())
        sale = Sale.objects.get(pk=self.sale.pk)
        self.assertIsNone(sale.created_by)
        self.assertEqual(sale.total, 5000)
        self.assertEqual(sale.cashier_name, "Marie Ngo")
        self.assertEqual(self.client.get(reverse("invoice", args=[sale.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse("sale_detail", args=[sale.pk])), "Marie Ngo")
        self.assertEqual(self.client.get(reverse("sale_list")).status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="user deleted", target="marie").exists())

    def test_old_sales_without_a_stored_name_get_it_at_delete_time(self):
        Sale.objects.filter(pk=self.sale.pk).update(created_by_name="")
        self.client.post(reverse("user_delete", args=[self.cashier.pk]))
        self.assertEqual(Sale.objects.get(pk=self.sale.pk).cashier_name, "Marie Ngo")

    def test_debt_payment_receipts_keep_who_received_them(self):
        customer = Customer.objects.create(name="Jean", phone="677111222")
        debt = Debt.objects.create(customer=customer, description="Oil", total=5000)
        payment = DebtPayment.objects.create(debt=debt, amount=1000, method="cash", received_by=self.cashier, received_by_name="Marie Ngo")
        self.client.post(reverse("user_delete", args=[self.cashier.pk]))
        self.assertContains(self.client.get(reverse("payment_receipt", args=[payment.pk])), "Marie Ngo")

    def test_confirm_page_explains_what_stays(self):
        res = self.client.get(reverse("user_delete", args=[self.cashier.pk]))
        self.assertContains(res, "1 sale")
        self.assertTrue(User.objects.filter(pk=self.cashier.pk).exists())  # GET never deletes

    def test_disabling_blocks_login_and_changes_nothing_else(self):
        self.client.post(reverse("user_toggle_active", args=[self.cashier.pk]))
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_active)
        self.assertEqual(Sale.objects.get(pk=self.sale.pk).created_by, self.cashier)
        self.client.logout()
        self.assertFalse(self.client.login(username="marie", password="x"))
        self.client.force_login(self.owner)
        self.client.post(reverse("user_toggle_active", args=[self.cashier.pk]))
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)

    def test_nobody_can_remove_themselves_or_the_last_boss(self):
        self.client.post(reverse("user_delete", args=[self.owner.pk]))
        self.client.post(reverse("user_toggle_active", args=[self.owner.pk]))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

        second = User.objects.create_user("boss2", password="x", role=User.OWNER)
        self.client.force_login(second)
        self.client.post(reverse("user_delete", args=[self.owner.pk]))  # fine: another boss remains
        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())
        self.client.post(reverse("user_toggle_active", args=[second.pk]))  # himself
        second.refresh_from_db()
        self.assertTrue(second.is_active)

    def test_last_boss_cannot_be_demoted_or_disabled_in_the_edit_form(self):
        res = self.client.post(reverse("user_update", args=[self.owner.pk]), {
            "username": "boss", "first_name": "", "last_name": "", "email": "", "role": "cashier", "is_active": "on",
        })
        self.assertEqual(res.status_code, 200)  # form re-shown with the error
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, User.OWNER)

    def test_cashier_cannot_use_these_pages(self):
        self.client.force_login(self.cashier)
        other = User.objects.create_user("x", password="x", role=User.CASHIER)
        self.assertEqual(self.client.post(reverse("user_delete", args=[other.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("user_toggle_active", args=[other.pk])).status_code, 403)
        self.assertTrue(User.objects.filter(pk=other.pk).exists())

    def test_user_page_renders(self):
        res = self.client.get(reverse("user_list"))
        self.assertContains(res, "Disable")
        self.assertContains(res, "Delete")
