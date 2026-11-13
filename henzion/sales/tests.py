import uuid
from datetime import timedelta
from decimal import Decimal
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

    def test_cashier_and_owner_can_sell_above_list_price(self):
        for user in (self.cashier, self.owner):
            sale = services.create_sale(user, self.payload(items=[{"product_id": self.product.pk, "quantity": 2, "price": 7000}]))
            item = sale.items.get()
            self.assertEqual(item.unit_price, 7000)
            self.assertEqual(item.discount, 0)
            self.assertEqual(item.amount, 14000)
            self.assertEqual(sale.total, 14000)

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


class UnpaidInvoiceTests(SalesTestBase):
    """Unpaid (proforma) invoices: given out to be paid; no stock, no sale, until the money is received."""

    def pf_payload(self, **overrides):
        data = {
            "customer": {"name": "ACME SARL", "phone": "677111222", "address": "Douala"},
            "items": [{"product_id": self.product.pk, "quantity": 2, "price": 5000}],
        }
        data.update(overrides)
        return data

    def make_pf(self, user=None, **overrides):
        return services.create_proforma(user or self.cashier, self.pf_payload(**overrides))

    def pay(self, pf, **overrides):
        item = pf.items.filter(is_sourced=False).first()
        data = {"payment_method": "cash", "proforma_id": pf.pk, "items": [
            {"product_id": item.product_id, "quantity": item.quantity, "price": item.net_price, "proforma_item_id": item.pk}]}
        data.update(overrides)
        return services.create_sale(self.cashier, data)

    # --- creating ---------------------------------------------------------------------------
    def test_creating_one_changes_no_stock_and_makes_no_sale_or_debt(self):
        from sales.models import Proforma

        pf = self.make_pf()
        self.assertEqual(pf.number, f"PRO-{pf.pk:06d}")
        self.assertEqual(pf.status, Proforma.OPEN)
        self.assertEqual(pf.total, 10000)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), 1)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Debt.objects.count(), 0)
        self.assertEqual(pf.valid_until, timezone.localdate() + timedelta(days=7))

    def test_item_keeps_its_category_name_for_the_record(self):
        from inventory.models import Category

        self.product.category = Category.objects.create(name="Toyota")
        self.product.save()
        self.assertEqual(self.make_pf().items.get().category_name, "Toyota")

    def test_customer_name_is_required_and_validity_is_limited(self):
        with self.assertRaises(services.SaleError):
            self.make_pf(customer={"name": " "})
        for days in (0, 91, "x"):
            with self.subTest(days=days), self.assertRaises(services.SaleError):
                self.make_pf(valid_days=days)
        self.assertEqual(self.make_pf(valid_days=30).valid_until, timezone.localdate() + timedelta(days=30))

    def test_same_request_twice_makes_one_invoice(self):
        from sales.models import Proforma

        u = str(uuid.uuid4())
        a, b = self.make_pf(client_uuid=u), self.make_pf(client_uuid=u)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Proforma.objects.count(), 1)

    def test_empty_invoice_is_rejected(self):
        with self.assertRaises(services.SaleError):
            self.make_pf(items=[])

    def test_cashier_price_rules_apply_to_unpaid_invoices_too(self):
        low = [{"product_id": self.product.pk, "quantity": 2, "price": 4000}]
        with self.assertRaises(services.SaleError):
            self.make_pf(items=low)  # needs the boss's approval
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        services.decide_discount(req, True, "ok")
        pf = self.make_pf(items=[dict(low[0], request_id=req.pk)])
        self.assertEqual(pf.total, 8000)
        self.assertEqual(pf.items.get().discount, 2000)
        with self.assertRaises(services.SaleError):  # the approval is used up
            self.make_pf(items=[dict(low[0], request_id=req.pk)])

    def test_neighbor_items_can_be_quoted_and_do_not_show_the_neighbor(self):
        pf = self.make_pf(sourced_items=[{"name": "Brake disc", "neighbor": "Mama Ngu", "quantity": 1, "cost": 1200, "price": 2000}])
        self.assertEqual(pf.total, 12000)
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("proforma_invoice", args=[pf.pk]))
        self.assertContains(res, "Brake disc")
        self.assertNotContains(res, "Mama Ngu")

    # --- paying ----------------------------------------------------------------------------
    def test_paying_it_makes_the_sale_takes_the_stock_and_marks_it_paid(self):
        from sales.models import Proforma

        pf = self.make_pf()
        sale = self.pay(pf)
        pf.refresh_from_db()
        self.assertEqual(pf.status, Proforma.PAID)
        self.assertEqual(pf.sale, sale)
        self.assertEqual(sale.total, 10000)
        self.assertEqual(self.stock(), 8)  # only now

    def test_the_quoted_discount_is_honoured_after_the_approval_has_expired(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        services.decide_discount(req, True, "ok")
        pf = self.make_pf(items=[{"product_id": self.product.pk, "quantity": 2, "price": 4000, "request_id": req.pk}])
        DiscountRequest.objects.filter(pk=req.pk).update(decided_at=timezone.now() - timedelta(days=3))
        sale = self.pay(pf)
        self.assertEqual(sale.total, 8000)
        self.assertEqual(sale.items.get().discount, 2000)

    def test_a_different_price_or_more_quantity_than_quoted_is_not_honoured(self):
        req = services.request_discount(self.cashier, self.product, 4000, 2)
        services.decide_discount(req, True, "ok")
        pf = self.make_pf(items=[{"product_id": self.product.pk, "quantity": 2, "price": 4000, "request_id": req.pk}])
        item = pf.items.get()
        base = {"payment_method": "cash", "proforma_id": pf.pk}
        for qty, price in ((2, 3000), (3, 4000)):
            with self.subTest(qty=qty, price=price), self.assertRaises(services.SaleError):
                services.create_sale(self.cashier, dict(base, items=[
                    {"product_id": self.product.pk, "quantity": qty, "price": price, "proforma_item_id": item.pk}]))
        self.assertEqual(self.stock(), 10)

    def test_a_cashier_cannot_use_someone_elses_quote_item_id_for_a_discount(self):
        pf = self.make_pf()  # full price
        item = pf.items.get()
        with self.assertRaises(services.SaleError):
            services.create_sale(self.cashier, {"payment_method": "cash", "proforma_id": pf.pk, "items": [
                {"product_id": self.product.pk, "quantity": 2, "price": 1000, "proforma_item_id": item.pk}]})

    def test_it_can_only_be_paid_once(self):
        pf = self.make_pf()
        self.pay(pf)
        with self.assertRaises(services.SaleError):
            self.pay(pf, client_uuid=str(uuid.uuid4()))
        self.assertEqual(self.stock(), 8)

    def test_cancelled_or_expired_ones_cannot_be_paid(self):
        from sales.models import Proforma

        gone = self.make_pf()
        services.cancel_proforma(self.cashier, gone)
        with self.assertRaises(services.SaleError):
            self.pay(gone)
        old = self.make_pf()
        Proforma.objects.filter(pk=old.pk).update(valid_until=timezone.localdate() - timedelta(days=1))
        old.refresh_from_db()
        self.assertTrue(old.is_expired)
        with self.assertRaises(services.SaleError):
            self.pay(old)
        self.assertEqual(self.stock(), 10)

    def test_not_enough_stock_at_payment_time_blocks_it(self):
        pf = self.make_pf(items=[{"product_id": self.product.pk, "quantity": 9, "price": 5000}])
        StockMovement.objects.create(product=self.product, kind=StockMovement.ADJUSTMENT, quantity=-5)
        with self.assertRaises(services.SaleError):
            self.pay(pf)
        pf.refresh_from_db()
        self.assertTrue(pf.is_open)

    def test_it_can_be_paid_in_split_or_credit_like_any_sale(self):
        pf = self.make_pf()
        sale = self.pay(
            pf, payments=[{"method": "cash", "amount": 6000}, {"method": "credit", "amount": 4000}],
            customer={"name": "ACME SARL", "phone": "677111222", "address": "Douala"},
            due_date=(timezone.localdate() + timedelta(days=10)).isoformat(),
        )
        self.assertEqual(Debt.objects.with_balance().get(sale=sale).total, 4000)

    def test_cancelling_leaves_stock_alone(self):
        pf = self.make_pf()
        services.cancel_proforma(self.cashier, pf)
        pf.refresh_from_db()
        self.assertEqual(pf.status, "cancelled")
        self.assertEqual(self.stock(), 10)
        with self.assertRaises(services.SaleError):
            services.cancel_proforma(self.cashier, pf)

    # --- not counted as sales -------------------------------------------------------------
    def test_unpaid_invoices_are_not_in_sales_dashboard_or_reports_until_paid(self):
        from reports.services import build_report

        pf = self.make_pf()
        today = timezone.localdate()
        self.client.force_login(self.owner)
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.context["today_total"], 0)
        self.assertEqual(res.context["unpaid"]["count"], 1)
        self.assertEqual(res.context["unpaid"]["total"], 10000)
        r = build_report(today, today)
        self.assertEqual(r["total_sales"], 0)
        self.assertEqual(r["sales_count"], 0)
        self.assertEqual(self.client.get(reverse("sale_list")).context["sales"].__len__(), 0)

        self.pay(pf)
        res = self.client.get(reverse("dashboard"))
        self.assertEqual(res.context["today_total"], 10000)
        self.assertEqual(res.context["unpaid"]["count"], 0)

    # --- pages ---------------------------------------------------------------------------
    def test_invoice_is_marked_unpaid_then_paid(self):
        pf = self.make_pf()
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("proforma_invoice", args=[pf.pk]))
        self.assertContains(res, "UNPAID")
        self.assertContains(res, "NOT YET PAID")
        self.assertContains(res, pf.number)
        self.assertContains(res, "ACME SARL")
        self.assertContains(res, "Valid Until")
        self.pay(pf)
        self.assertContains(self.client.get(reverse("proforma_invoice", args=[pf.pk])), "PAID")

    def test_pages_render_for_both_roles(self):
        pf = self.make_pf()
        for user in (self.cashier, self.owner):
            self.client.force_login(user)
            for name, args in (("proforma_list", []), ("proforma_detail", [pf.pk]), ("dashboard", []), ("pos", [])):
                with self.subTest(user=user.username, page=name):
                    self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        res = self.client.get(reverse("proforma_list") + "?status=all&q=ACME")
        self.assertContains(res, pf.number)

    def test_create_endpoint_returns_the_detail_page(self):
        import json

        self.client.force_login(self.cashier)
        res = self.client.post(reverse("proforma_create"), data=json.dumps(self.pf_payload()), content_type="application/json")
        self.assertTrue(res.json()["ok"])
        bad = self.client.post(reverse("proforma_create"), data=json.dumps(self.pf_payload(customer={"name": ""})), content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("proforma_create"), data="{}", content_type="application/json").status_code, 302)

    def test_till_loads_it_ready_to_pay_with_the_quoted_prices(self):
        pf = self.make_pf()
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("pos") + f"?proforma={pf.pk}")
        self.assertEqual(res.context["prefill"]["mode"], "pay")
        self.assertEqual(res.context["prefill"]["items"][0]["price"], 5000)
        self.assertEqual(res.context["prefill"]["items"][0]["item_id"], pf.items.get().pk)
        self.assertContains(res, "Payment received for unpaid invoice")
        self.assertNotContains(res, 'id="pf-btn"')  # can't make another unpaid invoice while paying one

    def test_a_paid_or_expired_one_does_not_load_for_payment_but_can_be_copied(self):
        from sales.models import Proforma

        pf = self.make_pf()
        Proforma.objects.filter(pk=pf.pk).update(valid_until=timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.cashier)
        self.assertIsNone(self.client.get(reverse("pos") + f"?proforma={pf.pk}").context["prefill"])
        Product.objects.filter(pk=self.product.pk).update(selling_price=5500)
        copy = self.client.get(reverse("pos") + f"?copy={pf.pk}").context["prefill"]
        self.assertEqual(copy["mode"], "copy")
        self.assertEqual(copy["items"][0]["price"], 5500)  # today's price
        self.assertIsNone(copy["items"][0]["item_id"])

    def test_cancel_permissions(self):
        pf = self.make_pf(user=self.cashier)
        other = User.objects.create_user("cash2", password="x", role=User.CASHIER)
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("proforma_cancel", args=[pf.pk])).status_code, 403)
        self.client.force_login(self.owner)
        self.client.post(reverse("proforma_cancel", args=[pf.pk]))
        pf.refresh_from_db()
        self.assertEqual(pf.status, "cancelled")

    def test_sale_page_links_back_to_the_unpaid_invoice(self):
        pf = self.make_pf()
        sale = self.pay(pf)
        self.client.force_login(self.cashier)
        self.assertContains(self.client.get(reverse("sale_detail", args=[sale.pk])), pf.number)


class PushHookTests(SalesTestBase):
    def test_boss_is_pushed_on_request_and_cashier_on_decision(self):
        from unittest import mock

        with mock.patch("sales.services.push.notify_users") as pushed:
            req = services.request_discount(self.cashier, self.product, 4000, 2)
            self.assertEqual(pushed.call_count, 1)
            self.assertIn("/sales/discount/decide/", pushed.call_args.kwargs["url"])
            services.decide_discount(req, True, "link")
            self.assertEqual(pushed.call_count, 2)
            self.assertEqual(pushed.call_args.args[0], [self.cashier])

    def test_owner_own_discount_sends_no_push(self):
        from unittest import mock

        with mock.patch("sales.services.push.notify_users") as pushed:
            services.request_discount(self.owner, self.product, 4000, 2)
            pushed.assert_not_called()


class HidePricesTests(UnpaidInvoiceTests):
    """Inherits the helpers only; the parent's own tests are excluded below."""

    def test_flag_is_saved_and_prices_are_left_out_of_the_page(self):
        pf = self.make_pf(hide_prices=True)
        self.assertTrue(pf.hide_prices)
        self.client.force_login(self.cashier)
        html = self.client.get(reverse("proforma_invoice", args=[pf.pk])).content.decode()
        self.assertIn("Engine oil", html)
        self.assertIn("Prices available on request", html)
        for needle in ("5 000", "5,000", "5000", "10 000", "10,000", "10000", "Subtotal"):
            self.assertNotIn(needle, html)

    def test_default_shows_prices(self):
        pf = self.make_pf()
        self.assertFalse(pf.hide_prices)
        self.client.force_login(self.cashier)
        html = self.client.get(reverse("proforma_invoice", args=[pf.pk])).content.decode()
        self.assertIn("Subtotal", html)

    def test_toggle_and_final_receipt_always_shows_prices(self):
        pf = self.make_pf(hide_prices=True)
        self.client.force_login(self.cashier)
        self.client.post(reverse("proforma_toggle_prices", args=[pf.pk]))
        pf.refresh_from_db()
        self.assertFalse(pf.hide_prices)
        self.client.post(reverse("proforma_toggle_prices", args=[pf.pk]))
        pf.refresh_from_db()
        self.assertTrue(pf.hide_prices)
        sale = self.pay(pf)
        html = self.client.get(reverse("invoice", args=[sale.pk])).content.decode()
        self.assertIn("Subtotal", html)
        self.assertNotIn("Prices available on request", html)


for _name in [n for n in dir(UnpaidInvoiceTests) if n.startswith("test_")]:
    setattr(HidePricesTests, _name, None)  # don't re-run the parent's tests in this class


class SaleAlertTests(SalesTestBase):
    def setUp(self):
        super().setUp()
        self.cs = CompanySettings.get()

    def sell(self, user=None, **kw):
        from unittest import mock

        with mock.patch("sales.services.push.notify_users") as pushed:
            sale = services.create_sale(user or self.cashier, self.payload(**kw))
        return sale, pushed

    def test_off_by_default(self):
        _, pushed = self.sell()
        pushed.assert_not_called()

    def test_on_sends_amount_and_cashier(self):
        self.cs.notify_every_sale = True
        self.cs.save()
        sale, pushed = self.sell()
        self.assertEqual(pushed.call_count, 1)
        self.assertIn("10", pushed.call_args.args[1])  # title carries the amount
        self.assertIn("cash", pushed.call_args.args[2])  # body names the cashier

    def test_threshold_skips_small_cash_sales_but_not_credit(self):
        self.cs.notify_every_sale = True
        self.cs.notify_sale_min = 50000
        self.cs.save()
        _, pushed = self.sell()
        pushed.assert_not_called()
        credit = self.payload(
            payment_method="credit", due_date=str(timezone.localdate() + timedelta(days=7)),
            customer={"name": "Jo", "phone": "677", "address": "Douala"},
        )
        from unittest import mock

        with mock.patch("sales.services.push.notify_users") as pushed2:
            services.create_sale(self.cashier, credit)
        self.assertEqual(pushed2.call_count, 1)

    def test_boss_own_sale_never_notifies(self):
        self.cs.notify_every_sale = True
        self.cs.save()
        _, pushed = self.sell(user=self.owner)
        pushed.assert_not_called()

    def test_cancellation_alert_and_switch(self):
        from unittest import mock

        sale = services.create_sale(self.cashier, self.payload())
        with mock.patch("sales.services.push.notify_users") as pushed:
            services.cancel_sale(self.cashier, sale)
        self.assertEqual(pushed.call_count, 1)
        sale2 = services.create_sale(self.cashier, self.payload())
        self.cs.notify_cancellations = False
        self.cs.save()
        with mock.patch("sales.services.push.notify_users") as pushed:
            services.cancel_sale(self.cashier, sale2)
        pushed.assert_not_called()


class HalfCartonTests(SalesTestBase):
    """Cartons sold by the half (stock 4.5) and cartons opened onto the shelf."""

    def setUp(self):
        super().setUp()
        from inventory.models import Product as P

        self.carton = self.product
        self.carton.name, self.carton.allow_half = "TOPISH 1L", True
        self.carton.selling_price, self.carton.cost_price = 15000, 12000
        self.carton.pieces_per_carton = 24
        self.carton.save()
        self.retail = P.objects.create(name="TOPISH 1L (RETAIL)", sku="TOPR", cost_price=500, selling_price=700)
        self.carton.retail_version = self.retail
        self.carton.save()

    def half(self, user=None, qty="0.5", price=15000, **kw):
        return services.create_sale(user or self.cashier, self.payload(
            items=[{"product_id": self.carton.pk, "quantity": qty, "price": price}], **kw))

    def test_half_carton_is_half_the_price_and_stock_goes_to_nine_and_a_half(self):
        sale = self.half()
        item = sale.items.get()
        self.assertEqual(item.quantity, Decimal("0.5"))
        self.assertEqual(item.amount, 7500)
        self.assertEqual(item.discount, 0)
        self.assertEqual(sale.total, 7500)
        self.assertEqual(self.stock(), Decimal("9.5"))
        self.assertEqual(item.profit, 7500 - 6000)  # half the carton cost

    def test_two_halves_make_a_carton_and_cancel_returns_the_half(self):
        s1, s2 = self.half(), self.half()
        self.assertEqual(self.stock(), 9)
        services.cancel_sale(self.owner, s1)
        self.assertEqual(self.stock(), Decimal("9.5"))

    def test_odd_price_half_rounds_up(self):
        self.carton.selling_price = 15001
        self.carton.save()
        self.assertEqual(self.half(price=15001).items.get().amount, 7501)

    def test_cannot_sell_half_of_a_product_that_does_not_allow_it(self):
        self.carton.allow_half = False
        self.carton.save()
        with self.assertRaises(services.SaleError):
            self.half()

    def test_other_fractions_rejected(self):
        for bad in ("0.3", "1.25", "0", "-1"):
            with self.assertRaises(services.SaleError):
                self.half(qty=bad)

    def test_reduced_half_carton_price_still_needs_approval(self):
        with self.assertRaises(services.SaleError):
            self.half(price=14000)

    def test_cannot_sell_more_than_stock_with_halves(self):
        with self.assertRaises(services.SaleError):
            self.half(qty="10.5")

    def test_unpaid_invoice_with_half_carton(self):
        pf = services.create_proforma(self.cashier, {
            "customer": {"name": "ACME"}, "items": [{"product_id": self.carton.pk, "quantity": 1.5, "price": 15000}]})
        self.assertEqual(pf.total, 22500)
        self.assertEqual(self.stock(), 10)
        item = pf.items.get()
        sale = services.create_sale(self.cashier, {"payment_method": "cash", "proforma_id": pf.pk, "items": [
            {"product_id": self.carton.pk, "quantity": 1.5, "price": item.net_price, "proforma_item_id": item.pk}]})
        self.assertEqual(sale.total, 22500)
        self.assertEqual(self.stock(), Decimal("8.5"))

    def test_open_carton_moves_pieces_to_the_shelf(self):
        from inventory.services import StockError, open_carton

        result = open_carton(self.cashier, self.carton.pk, 1)
        self.assertEqual(result["pieces"], 24)
        self.assertEqual(Product.objects.get(pk=self.carton.pk).stock, 9)
        self.assertEqual(Product.objects.get(pk=self.retail.pk).stock, 24)
        moves = StockMovement.objects.filter(kind=StockMovement.TRANSFER)
        self.assertEqual(moves.count(), 2)
        with self.assertRaises(StockError):
            open_carton(self.cashier, self.carton.pk, 50)

    def test_cannot_open_a_half_carton_or_unlinked_product(self):
        from inventory.services import StockError, open_carton

        self.half(qty="9.5")  # leaves half a carton
        with self.assertRaises(StockError):
            open_carton(self.cashier, self.carton.pk, 1)
        with self.assertRaises(StockError):
            open_carton(self.cashier, self.retail.pk, 1)

    def test_open_carton_endpoint_for_the_till(self):
        self.client.force_login(self.cashier)
        r = self.client.post(reverse("open_carton"), {"product_id": self.carton.pk, "count": 1}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["retail_stock"], 24)
        r = self.client.post(reverse("open_carton"), {"product_id": self.carton.pk, "count": 99}, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_short_shelf_error_mentions_opening_a_carton(self):
        with self.assertRaises(services.SaleError) as ctx:
            services.create_sale(self.cashier, self.payload(items=[{"product_id": self.retail.pk, "quantity": 1, "price": 700}]))
        self.assertIn("Open a carton", str(ctx.exception))

    def test_stock_worth_counts_the_half(self):
        from inventory.services import stock_valuation

        self.half(qty="0.5")
        row = next(r for r in stock_valuation()["rows"] if r["product"].pk == self.carton.pk)
        self.assertEqual(row["cost_value"], 12000 * 9 + 6000)
