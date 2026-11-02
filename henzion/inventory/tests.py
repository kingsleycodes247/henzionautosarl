from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import Category, Product, StockMovement
from inventory.services import stock_valuation

User = get_user_model()


class StockWorthTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("cash", password="x", role=User.CASHIER)
        oils = Category.objects.create(name="Oils")
        self.oil = Product.objects.create(name="Engine oil", sku="OIL", category=oils, cost_price=3000, selling_price=5000)
        self.battery = Product.objects.create(name="Battery", sku="BAT", cost_price=40000, selling_price=55000)
        StockMovement.objects.create(product=self.oil, kind="opening", quantity=10)
        StockMovement.objects.create(product=self.battery, kind="opening", quantity=2)

    def test_worth_at_cost_and_selling_price(self):
        t = stock_valuation()["totals"]
        self.assertEqual(t["units"], 12)
        self.assertEqual(t["cost"], 10 * 3000 + 2 * 40000)       # 110 000
        self.assertEqual(t["retail"], 10 * 5000 + 2 * 55000)     # 160 000
        self.assertEqual(t["profit"], 50000)

    def test_sales_reduce_the_worth(self):
        StockMovement.objects.create(product=self.oil, kind="sale", quantity=-4)
        t = stock_valuation()["totals"]
        self.assertEqual(t["cost"], 6 * 3000 + 80000)

    def test_negative_stock_and_inactive_products_are_not_counted(self):
        StockMovement.objects.create(product=self.battery, kind="adjustment", quantity=-5)  # now -3
        Product.objects.filter(pk=self.oil.pk).update(is_active=False)
        t = stock_valuation()["totals"]
        self.assertEqual((t["units"], t["cost"], t["retail"]), (0, 0, 0))

    def test_products_without_a_cost_price_are_flagged(self):
        Product.objects.filter(pk=self.oil.pk).update(cost_price=0)
        self.assertEqual(stock_valuation()["totals"]["missing_cost"], 1)

    def test_categories_are_grouped(self):
        names = {c["name"]: c["cost"] for c in stock_valuation()["categories"]}
        self.assertEqual(names, {"Oils": 30000, "No category": 80000})

    def test_owner_sees_the_page_dashboard_card_and_product_list_strip(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("stock_value"))
        self.assertContains(page, "110 000")
        self.assertContains(page, "160 000")
        self.assertContains(self.client.get(reverse("dashboard")), "Stock worth")
        self.assertContains(self.client.get(reverse("product_list")), "160 000")
        self.assertContains(self.client.get(reverse("stock_value"), {"q": "battery"}), "Battery")

    def test_cashier_cannot_see_worth_or_cost_prices(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("stock_value")).status_code, 403)
        listing = self.client.get(reverse("product_list"))
        self.assertEqual(listing.status_code, 200)
        self.assertNotContains(listing, "40 000")  # the battery's cost price
        self.assertNotContains(listing, "Stock worth")
