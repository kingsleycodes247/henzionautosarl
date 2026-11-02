import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import CompanySettings
from debts.models import Debt, DebtPayment
from inventory.models import Product, StockMovement
from sales import services
from sales.models import DiscountRequest, Sale

User = get_user_model()


def run_now(func, *args, **kwargs):
    """Stand-in for the background thread so tests are deterministic."""
    func(*args, **kwargs)


class SalesTestBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("cash", password="x", role=User.CASHIER)
        self.product = Product.objects.create(name="Engine oil", sku="OIL", cost_price=3000, selling_price=5000)
        StockMovement.objects.create(product=self.product, kind=StockMovement.OPENING, quantity=10)
        cs = CompanySettings.get()
        cs.boss_email = ""
        cs.boss_whatsapp = ""
        cs.save()
        patcher = mock.patch("sales.services.notify.run_in_background", side_effect=run_now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def payload(self, **overrides):
        data = {
            "payment_method": "cash",
            "items": [{"product_id": self.product.pk, "quantity": 2, "price": 5000}],
        }
        data.update(overrides)
        return data

    def stock(self):
        return Product.objects.get(pk=self.product.pk).stock


class SaleTests(SalesTestBase):
    def test_cash_sale_deducts_stock_and_totals(self):
        sale = services.create_sale(self.cashier, self.payload())
        self.assertEqual(sale.total, 10000)
        self.assertTrue(sale.number.startswith("INV-"))
        self.assertEqual(self.stock(), 8)

    def test_all_four_methods_are_accepted(self):
        for method in ("cash", "mtn_momo", "orange_money"):
            sale = services.create_sale(self.cashier, self.payload(payment_method=method, payment_reference="REF1"))
            self.assertEqual(sale.payment_method, method)

    def test_not_enough_stock(self):
        items = [{"product_id": self.product.pk, "quantity": 11, "price": 5000}]
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.payload(items=items))
        self.assertEqual(Sale.objects.count(), 0)

    def test_same_client_uuid_does_not_duplicate(self):
        token = str(uuid.uuid4())
        first = services.create_sale(self.cashier, self.payload(client_uuid=token))
        second = services.create_sale(self.cashier, self.payload(client_uuid=token))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), 8)

    def test_vat_is_added_when_configured(self):
        cs = CompanySettings.get()
        cs.vat_rate = 19.25
        cs.save()
        sale = services.create_sale(self.cashier, self.payload())
        self.assertEqual(sale.subtotal, 10000)
        self.assertEqual(sale.vat_amount, 1925)
        self.assertEqual(sale.total, 11925)

    def test_cancel_returns_stock(self):
        sale = services.create_sale(self.cashier, self.payload())
        services.cancel_sale(self.owner, sale)
        self.assertEqual(self.stock(), 10)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.CANCELLED)


class DiscountApprovalTests(SalesTestBase):
    def reduced(self, request_id=None, price=4000):
        return self.payload(items=[{"product_id": self.product.pk, "quantity": 2, "price": price, "request_id": request_id}])

    def test_cashier_cannot_sell_below_list_price_without_approval(self):
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.reduced())

    def test_pending_request_cannot_be_used(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2, "regular customer")
        self.assertEqual(req.status, DiscountRequest.PENDING)
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.reduced(req.pk))

    def test_approved_request_applies_new_price_once(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        self.assertTrue(services.decide_discount(req, True, "test"))
        sale = services.create_sale(self.cashier, self.reduced(req.pk))
        self.assertEqual(sale.total, 8000)
        self.assertEqual(sale.items.get().discount, 2000)
        with self.assertRaises(services.SaleError):  # already consumed
            services.create_sale(self.cashier, self.reduced(req.pk))

    def test_declined_request_keeps_original_price(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        services.decide_discount(req, False, "test")
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.reduced(req.pk))

    def test_approval_is_tied_to_the_exact_price(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        services.decide_discount(req, True, "test")
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.reduced(req.pk, price=3000))

    def test_expired_request_cannot_be_approved(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        DiscountRequest.objects.filter(pk=req.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        req.refresh_from_db()
        self.assertFalse(services.decide_discount(req, True, "late"))
        req.refresh_from_db()
        self.assertEqual(req.status, DiscountRequest.EXPIRED)

    def test_owner_discount_is_approved_instantly(self):
        req = services.request_discount(self.owner, self.product, 4500, 1)
        self.assertEqual(req.status, DiscountRequest.APPROVED)

    def test_auto_approve_rule(self):
        cs = CompanySettings.get()
        cs.auto_approve_percent = 10
        cs.save()
        small = services.request_discount(self.cashier, self.product, 4600, 1)  # 8%
        big = services.request_discount(self.cashier, self.product, 4000, 1)  # 20%
        self.assertEqual(small.status, DiscountRequest.APPROVED)
        self.assertEqual(big.status, DiscountRequest.PENDING)

    def test_price_must_be_lower_than_list(self):
        with self.assertRaises(services.SaleError):
            services.request_discount(self.cashier, self.product, 5000, 1)

    def test_link_page_needs_a_button_press_and_decides(self):
        req = services.request_discount(self.cashier, self.product, 4000, 1)
        url = reverse("discount_decide", args=[services.make_token(req)])
        self.assertEqual(self.client.get(url).status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, DiscountRequest.PENDING)  # a plain GET (link preview) changes nothing
        self.client.post(url, {"action": "approve"})
        req.refresh_from_db()
        self.assertEqual(req.status, DiscountRequest.APPROVED)

    def test_tampered_link_is_rejected(self):
        req = services.request_discount(self.cashier, self.product, 4000, 1)
        url = reverse("discount_decide", args=[services.make_token(req) + "x"])
        self.client.post(url, {"action": "approve"})
        req.refresh_from_db()
        self.assertEqual(req.status, DiscountRequest.PENDING)


class CreditSaleTests(SalesTestBase):
    def credit_payload(self, **overrides):
        data = self.payload(
            payment_method="credit",
            customer={"name": "Jean Mbarga", "phone": "677111222", "address": "Bonamoussadi, Douala"},
            due_date=(timezone.localdate() + timedelta(days=14)).isoformat(),
        )
        data.update(overrides)
        return data

    def test_credit_needs_customer_details_and_due_date(self):
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.payload(payment_method="credit"))
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.credit_payload(due_date=""))

    def test_full_credit_sale_creates_debt_and_payments_settle_it(self):
        sale = services.create_sale(self.cashier, self.credit_payload())
        self.assertEqual(sale.payment_method, "credit")
        debt = Debt.objects.with_balance().get(sale=sale)
        self.assertEqual(debt.total, 10000)
        self.assertEqual(debt.balance_amount, 10000)
        self.assertEqual(debt.status, "open")
        self.assertEqual(debt.customer.phone, "677111222")

        DebtPayment.objects.create(debt=debt, amount=4000, method="cash")
        self.assertEqual(Debt.objects.with_balance().get(pk=debt.pk).status, "partial")
        DebtPayment.objects.create(debt=debt, amount=6000, method="orange_money")
        debt = Debt.objects.with_balance().get(pk=debt.pk)
        self.assertEqual(debt.balance_amount, 0)
        self.assertEqual(debt.status, "paid")

    def test_repeat_customer_is_reused_by_phone(self):
        services.create_sale(self.cashier, self.credit_payload())
        services.create_sale(self.cashier, self.credit_payload())
        from debts.models import Customer
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Debt.objects.count(), 2)

    def test_credit_limit_is_enforced(self):
        services.create_sale(self.cashier, self.credit_payload())
        from debts.models import Customer
        Customer.objects.update(credit_limit=15000)
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.credit_payload())  # 10 000 + 10 000 > 15 000

    def test_cancelling_a_credit_sale_removes_its_debt(self):
        sale = services.create_sale(self.cashier, self.credit_payload())
        services.cancel_sale(self.owner, sale)
        self.assertEqual(Debt.objects.count(), 0)
        self.assertEqual(self.stock(), 10)


class SplitPaymentTests(SalesTestBase):
    """One sale paid several ways: 10 000 = 5 000 cash + 3 000 Orange Money + 2 000 credit."""

    def split_payload(self, payments=None, **overrides):
        data = self.payload(
            payments=payments or [
                {"method": "cash", "amount": 5000},
                {"method": "orange_money", "amount": 3000, "reference": "OM-77"},
                {"method": "credit", "amount": 2000},
            ],
            customer={"name": "Jean Mbarga", "phone": "677111222", "address": "Bonamoussadi, Douala"},
            due_date=(timezone.localdate() + timedelta(days=14)).isoformat(),
        )
        data.update(overrides)
        return data

    def test_the_example_split_sale(self):
        from sales.models import SalePayment

        sale = services.create_sale(self.cashier, self.split_payload())
        self.assertEqual(sale.total, 10000)
        self.assertEqual(sale.payment_method, "split")
        self.assertEqual(self.stock(), 8)

        lines = list(SalePayment.objects.filter(sale=sale).values_list("method", "amount", "reference"))
        self.assertEqual(lines, [("cash", 5000, ""), ("orange_money", 3000, "OM-77"), ("credit", 2000, "")])

        # only the credit part is owed
        debt = Debt.objects.with_balance().get(sale=sale)
        self.assertEqual(debt.total, 2000)
        self.assertEqual(debt.balance_amount, 2000)
        DebtPayment.objects.create(debt=debt, amount=2000, method="mtn_momo")
        self.assertTrue(Debt.objects.with_balance().get(pk=debt.pk).is_paid)

    def test_payments_must_add_up_to_the_total(self):
        short = [{"method": "cash", "amount": 5000}, {"method": "orange_money", "amount": 3000}]  # 8 000
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.split_payload(payments=short))
        over = [{"method": "cash", "amount": 11000}]
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.split_payload(payments=over))
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock(), 10)

    def test_split_without_credit_makes_no_debt_and_needs_no_customer(self):
        payments = [{"method": "cash", "amount": 6000}, {"method": "mtn_momo", "amount": 4000, "reference": "MP1"}]
        data = self.payload(payments=payments)
        sale = services.create_sale(self.cashier, data)
        self.assertEqual(sale.payment_method, "split")
        self.assertEqual(Debt.objects.count(), 0)

    def test_credit_part_still_needs_customer_details_and_due_date(self):
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.split_payload(customer={}))
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.split_payload(due_date=""))

    def test_same_method_entered_twice_is_added_together(self):
        payments = [{"method": "cash", "amount": 4000}, {"method": "cash", "amount": 6000}]
        sale = services.create_sale(self.cashier, self.payload(payments=payments))
        self.assertEqual(sale.payment_method, "cash")
        self.assertEqual(sale.payments.count(), 1)
        self.assertEqual(sale.payments.get().amount, 10000)

    def test_unknown_method_and_negative_amounts_are_rejected(self):
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.payload(payments=[{"method": "bitcoin", "amount": 10000}]))
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.payload(payments=[{"method": "cash", "amount": -1}, {"method": "mtn_momo", "amount": 10001}]))

    def test_credit_limit_counts_only_the_credit_part(self):
        from debts.models import Customer

        services.create_sale(self.cashier, self.split_payload())  # owes 2 000
        Customer.objects.update(credit_limit=3000)
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, self.split_payload())  # 2 000 + 2 000 > 3 000
        cash_only = [{"method": "cash", "amount": 10000}]
        services.create_sale(self.cashier, self.payload(payments=cash_only))  # no new credit: fine

    def test_sales_list_filter_finds_split_sales_by_any_method(self):
        sale = services.create_sale(self.cashier, self.split_payload())
        self.client.force_login(self.owner)
        for method in ("cash", "orange_money", "credit"):
            res = self.client.get(reverse("sale_list"), {"method": method})
            self.assertContains(res, sale.number)
        res = self.client.get(reverse("sale_list"), {"method": "mtn_momo"})
        self.assertNotContains(res, sale.number)

    def test_invoice_and_detail_show_every_payment_line(self):
        sale = services.create_sale(self.cashier, self.split_payload())
        self.client.force_login(self.owner)
        invoice = self.client.get(reverse("invoice", args=[sale.pk]))
        for text in ("Cash 5 000", "Orange Money 3 000", "Credit 2 000", "OM-77"):
            self.assertContains(invoice, text)
        self.assertContains(self.client.get(reverse("sale_detail", args=[sale.pk])), "Orange Money")

    def test_pos_endpoint_accepts_split_payments(self):
        import json

        self.client.force_login(self.cashier)
        res = self.client.post(reverse("pos"), data=json.dumps(self.split_payload()), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        bad = self.split_payload(payments=[{"method": "cash", "amount": 1}])
        res = self.client.post(reverse("pos"), data=json.dumps(bad), content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("add up", res.json()["error"])

    def test_cancelling_a_split_sale_returns_stock_and_removes_its_debt(self):
        sale = services.create_sale(self.cashier, self.split_payload())
        services.cancel_sale(self.owner, sale)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(Debt.objects.count(), 0)


class PosEndpointTests(SalesTestBase):
    def test_pos_post_returns_json_and_redirect(self):
        import json
        self.client.force_login(self.cashier)
        res = self.client.post(reverse("pos"), data=json.dumps(self.payload()), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    def test_pos_page_and_invoice_render(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("pos")).status_code, 200)
        sale = services.create_sale(self.cashier, self.payload())
        res = self.client.get(reverse("invoice", args=[sale.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "M052217397952Y")
        self.assertContains(res, "673 441 221")
        self.assertContains(res, sale.number)

    def test_cashier_cannot_open_someone_elses_invoice(self):
        sale = services.create_sale(self.owner, self.payload())
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("invoice", args=[sale.pk])).status_code, 403)


class NeighborItemTests(SalesTestBase):
    """Items the shop doesn't stock, fetched from a neighbor: on the invoice, outside stock and sales."""

    def neighbor_line(self, **overrides):
        line = {"name": "Brake disc", "neighbor": "Mama Ngu", "quantity": 1, "cost": 1200, "price": 2000}
        line.update(overrides)
        return line

    def mixed_payload(self, **overrides):
        data = self.payload(sourced_items=[self.neighbor_line()])
        data.update(overrides)
        return data

    def test_neighbor_only_sale_touches_no_stock_and_no_product(self):
        from sales.models import SaleItem

        products_before = Product.objects.count()
        sale = services.create_sale(self.cashier, {"payment_method": "cash", "items": [], "sourced_items": [self.neighbor_line()]})
        self.assertEqual(sale.total, 2000)
        self.assertEqual(sale.sourced_total, 2000)
        self.assertEqual(sale.own_total, 0)
        self.assertEqual(Product.objects.count(), products_before)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)  # just the opening stock
        item = SaleItem.objects.get(sale=sale)
        self.assertTrue(item.is_sourced)
        self.assertIsNone(item.product)
        self.assertEqual(item.profit, 800)

    def test_mixed_sale_splits_own_sales_from_neighbor_money(self):
        sale = services.create_sale(self.cashier, self.mixed_payload())
        self.assertEqual(sale.total, 12000)
        self.assertEqual(sale.sourced_total, 2000)
        self.assertEqual(sale.own_total, 10000)
        self.assertEqual(self.stock(), 8)

    def test_selling_below_the_neighbors_price_is_a_recorded_loss(self):
        from sales.neighbors import profit_summary

        services.create_sale(self.cashier, self.payload(sourced_items=[self.neighbor_line(cost=1500, price=1000, quantity=2)]))
        today = timezone.localdate()
        summary = profit_summary(today, today)
        self.assertEqual(summary["neighbor_profit"], -1000)
        self.assertEqual(summary["neighbor_loss"], 1000)
        self.assertEqual(summary["neighbor_loss_lines"], 1)
        self.assertEqual(summary["own_profit"], 4000)
        self.assertEqual(summary["profit"], 3000)

    def test_vat_is_shared_so_own_and_neighbor_parts_add_up(self):
        cs = CompanySettings.get()
        cs.vat_rate = 19.25
        cs.save()
        sale = services.create_sale(self.cashier, self.mixed_payload())
        self.assertEqual(sale.subtotal, 12000)
        self.assertEqual(sale.total, sale.subtotal + sale.vat_amount)
        self.assertGreater(sale.sourced_total, 2000)
        self.assertEqual(sale.own_total + sale.sourced_total, sale.total)

    def test_neighbor_is_reused_whatever_the_capitalisation(self):
        from sales.models import Neighbor

        services.create_sale(self.cashier, self.payload(sourced_items=[self.neighbor_line(neighbor="Mama Ngu")]))
        services.create_sale(self.cashier, self.payload(sourced_items=[self.neighbor_line(neighbor="  mama ngu ")]))
        self.assertEqual(Neighbor.objects.count(), 1)

    def test_bad_neighbor_lines_are_rejected_and_nothing_is_saved(self):
        for bad in (
            self.neighbor_line(name=""), self.neighbor_line(neighbor=""), self.neighbor_line(quantity=0),
            self.neighbor_line(cost=-5), self.neighbor_line(price=0), self.neighbor_line(price="abc"),
        ):
            with self.subTest(line=bad), self.assertRaises(services.SaleError):
                services.create_sale(self.cashier, self.payload(sourced_items=[bad]))
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.stock(), 10)

    def test_a_sale_needs_at_least_one_item(self):
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, {"payment_method": "cash", "items": [], "sourced_items": []})

    def test_split_and_credit_work_with_neighbor_items(self):
        data = self.mixed_payload(
            payments=[{"method": "cash", "amount": 7000}, {"method": "orange_money", "amount": 3000}, {"method": "credit", "amount": 2000}],
            customer={"name": "Jean Mbarga", "phone": "677111222", "address": "Douala"},
            due_date=(timezone.localdate() + timedelta(days=14)).isoformat(),
        )
        sale = services.create_sale(self.cashier, data)
        self.assertEqual(sale.total, 12000)
        self.assertEqual(Debt.objects.with_balance().get(sale=sale).total, 2000)

    def test_cancelling_removes_neighbor_lines_without_touching_stock(self):
        from sales.neighbors import profit_summary

        sale = services.create_sale(self.cashier, self.mixed_payload())
        self.assertEqual(self.stock(), 8)
        services.cancel_sale(self.owner, sale)
        self.assertEqual(self.stock(), 10)  # only the 2 own items came back
        today = timezone.localdate()
        self.assertEqual(profit_summary(today, today)["neighbor_lines"], 0)

    def test_invoice_shows_the_item_but_not_the_neighbor_or_the_cost(self):
        sale = services.create_sale(self.cashier, self.mixed_payload(sourced_items=[self.neighbor_line(cost=1234)]))
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("invoice", args=[sale.pk]))
        self.assertContains(res, "Brake disc")
        self.assertNotContains(res, "Mama Ngu")
        self.assertNotContains(res, "1 234")
        self.assertNotContains(res, "1,234")

    def test_cashier_sale_page_hides_neighbor_name_and_profit_but_owner_sees_them(self):
        sale = services.create_sale(self.cashier, self.mixed_payload())
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("sale_detail", args=[sale.pk]))
        self.assertContains(res, "Brake disc")
        self.assertNotContains(res, "Mama Ngu")
        self.assertNotContains(res, "not counted in sales")
        self.client.force_login(self.owner)
        res = self.client.get(reverse("sale_detail", args=[sale.pk]))
        self.assertContains(res, "Mama Ngu")
        self.assertContains(res, "not counted in sales")

    def test_dashboard_leaves_neighbor_items_out_of_sales_but_counts_profit_and_debt_to_neighbors(self):
        services.create_sale(self.cashier, self.mixed_payload())
        self.client.force_login(self.owner)
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["today_total"], 10000)
        self.assertEqual(res.context["today_count"], 1)
        self.assertEqual(res.context["today_neighbor_money"], 2000)
        self.assertEqual(res.context["profit_today"]["profit"], 4800)
        self.assertEqual(res.context["profit_today"]["neighbor_profit"], 800)
        self.assertEqual(res.context["owed"]["total"], 1200)

    def test_neighbor_only_sale_is_not_counted_as_a_sale_on_the_dashboard(self):
        services.create_sale(self.cashier, {"payment_method": "cash", "items": [], "sourced_items": [self.neighbor_line()]})
        self.client.force_login(self.owner)
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.context["today_total"], 0)
        self.assertEqual(res.context["today_count"], 0)
        self.assertEqual(res.context["profit_today"]["profit"], 800)

    def test_cashier_dashboard_still_renders(self):
        services.create_sale(self.cashier, self.mixed_payload())
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_neighbor_page_is_owner_only(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("neighbor_items")).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("neighbor_items")).status_code, 200)

    def test_neighbor_table_lists_items_and_filters(self):
        services.create_sale(self.cashier, self.mixed_payload())
        services.create_sale(self.cashier, self.payload(sourced_items=[self.neighbor_line(name="Fan belt", neighbor="Pa Joe", cost=900, price=500)]))
        self.client.force_login(self.owner)
        res = self.client.get(reverse("neighbor_items"))
        self.assertContains(res, "Brake disc")
        self.assertContains(res, "Fan belt")
        res = self.client.get(reverse("neighbor_items"), {"loss": "1"})
        self.assertContains(res, "Fan belt")
        self.assertNotContains(res, "Brake disc")
        self.assertEqual(res.context["totals"]["loss"], 400)

    def test_marking_paid_undoing_and_settling_a_neighbor(self):
        from sales.models import SaleItem
        from sales.neighbors import owed_by_neighbor

        sale = services.create_sale(self.cashier, self.payload(sourced_items=[self.neighbor_line(), self.neighbor_line(name="Fan belt", cost=300, price=600)]))
        first, second = SaleItem.objects.filter(sale=sale, is_sourced=True).order_by("id")
        self.assertEqual(owed_by_neighbor()["total"], 1500)

        self.client.force_login(self.owner)
        self.client.post(reverse("neighbor_item_paid", args=[first.pk]))
        self.assertEqual(owed_by_neighbor()["total"], 300)
        self.client.post(reverse("neighbor_item_paid", args=[first.pk]))  # undo
        self.assertEqual(owed_by_neighbor()["total"], 1500)

        self.client.post(reverse("neighbor_settle", args=[first.neighbor_id]))
        self.assertEqual(owed_by_neighbor()["total"], 0)
        second.refresh_from_db()
        self.assertIsNotNone(second.neighbor_paid_on)

    def test_cashier_cannot_mark_paid_or_settle(self):
        sale = services.create_sale(self.cashier, self.mixed_payload())
        item = sale.items.get(is_sourced=True)
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.post(reverse("neighbor_item_paid", args=[item.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("neighbor_settle", args=[item.neighbor_id])).status_code, 403)
        item.refresh_from_db()
        self.assertIsNone(item.neighbor_paid_on)

    def test_boss_can_correct_the_neighbors_price_and_profit_follows(self):
        from sales.neighbors import profit_summary

        sale = services.create_sale(self.cashier, self.mixed_payload())
        item = sale.items.get(is_sourced=True)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("neighbor_item_update", args=[item.pk])).status_code, 200)
        res = self.client.post(reverse("neighbor_item_update", args=[item.pk]), {"neighbor": item.neighbor_id, "unit_cost": 1500, "neighbor_paid_on": ""})
        self.assertEqual(res.status_code, 302)
        today = timezone.localdate()
        self.assertEqual(profit_summary(today, today)["neighbor_profit"], 500)

    def test_pos_endpoint_accepts_neighbor_items(self):
        import json

        self.client.force_login(self.cashier)
        res = self.client.post(reverse("pos"), data=json.dumps(self.mixed_payload()), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertContains(self.client.get(reverse("pos")), "Item from a neighbor")
