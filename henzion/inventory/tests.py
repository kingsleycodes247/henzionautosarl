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


class CategorySearchTests(TestCase):
    """Same-named products are told apart by category; typing a category lists everything in it."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from inventory.models import Category

        User = get_user_model()
        self.owner = User.objects.create_user("boss", password="x", role=User.OWNER)
        self.cashier = User.objects.create_user("cash", password="x", role=User.CASHIER)
        self.toyota, self.honda = Category.objects.create(name="Toyota"), Category.objects.create(name="Honda")
        mk = lambda name, sku, cat, stock: self._make(name, sku, cat, stock)
        self.a = mk("Brake pad", "BP-T", self.toyota, 5)
        self.b = mk("Brake pad", "BP-H", self.honda, 4)
        self.c = mk("Oil filter", "OF-H", self.honda, 7)
        self.d = mk("Wiper", "WP", None, 2)

    def _make(self, name, sku, category, stock):
        p = Product.objects.create(name=name, sku=sku, category=category, cost_price=1000, selling_price=2000)
        StockMovement.objects.create(product=p, kind=StockMovement.OPENING, quantity=stock)
        return p

    def names(self, res):
        return sorted(p.sku for p in res.context["products"])

    def test_typing_a_category_name_lists_its_items(self):
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("product_list"), {"q": "honda"})
        self.assertEqual(self.names(res), ["BP-H", "OF-H"])

    def test_category_dropdown_and_no_category(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.names(self.client.get(reverse("product_list"), {"category": self.toyota.pk})), ["BP-T"])
        self.assertEqual(self.names(self.client.get(reverse("product_list"), {"category": "none"})), ["WP"])
        both = self.client.get(reverse("product_list"), {"category": self.honda.pk, "q": "brake"})
        self.assertEqual(self.names(both), ["BP-H"])

    def test_product_list_shows_the_category(self):
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("product_list"), {"q": "brake"})
        self.assertContains(res, "Toyota")
        self.assertContains(res, "Honda")

    def test_stock_worth_filters_by_category_and_totals_the_selection(self):
        self.client.force_login(self.owner)
        res = self.client.get(reverse("stock_value"), {"category": self.honda.pk})
        self.assertEqual(sorted(r["product"].sku for r in res.context["rows"]), ["BP-H", "OF-H"])
        self.assertEqual(res.context["selection"]["qty"], 11)
        self.assertEqual(res.context["selection"]["retail_value"], 11 * 2000)
        typed = self.client.get(reverse("stock_value"), {"q": "toyota"})
        self.assertEqual([r["product"].sku for r in typed.context["rows"]], ["BP-T"])
        self.assertIsNone(self.client.get(reverse("stock_value")).context["selection"])

    def test_till_receives_the_category_of_every_product(self):
        self.client.force_login(self.cashier)
        res = self.client.get(reverse("pos"))
        cats = {p["sku"]: p["category"] for p in res.context["products"]}
        self.assertEqual(cats, {"BP-T": "Toyota", "BP-H": "Honda", "OF-H": "Honda", "WP": ""})
        self.assertContains(res, 'id="cat-filter"')

    def test_category_page_links_to_its_items(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("category_list")), f"?category={self.toyota.pk}")
