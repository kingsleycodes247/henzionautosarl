from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import CompanySettings
from debts.models import Customer, Debt, DebtPayment
from inventory.models import Category, Product, StockMovement

User = get_user_model()

PRODUCTS = [
    # name, sku, category, cost, price, stock, reorder
    ("Engine oil 5W-30 (1 L)", "OIL-5W30-1L", "Oils & fluids", 3500, 5000, 40, 10),
    ("Engine oil 15W-40 (4 L)", "OIL-15W40-4L", "Oils & fluids", 11000, 15000, 25, 6),
    ("Transmission fluid ATF (1 L)", "ATF-1L", "Oils & fluids", 4000, 6000, 18, 6),
    ("Brake fluid DOT4 (500 ml)", "BRK-DOT4", "Oils & fluids", 1800, 3000, 30, 8),
    ("Car battery 60Ah", "BAT-60AH", "Batteries", 42000, 55000, 8, 3),
    ("Car battery 100Ah", "BAT-100AH", "Batteries", 70000, 88000, 4, 2),
    ("Oil filter (universal)", "FLT-OIL", "Filters", 1500, 2500, 60, 15),
    ("Air filter (Toyota Corolla)", "FLT-AIR-COR", "Filters", 3000, 5000, 20, 5),
    ("Brake pads front (Toyota)", "BRK-PAD-TOY", "Brakes", 9000, 14000, 12, 4),
    ("Spark plug (set of 4)", "SPK-4", "Ignition", 4500, 7500, 3, 5),
    ("Wiper blade (pair)", "WIP-PR", "Accessories", 2000, 3500, 25, 6),
]


class Command(BaseCommand):
    help = "Create demo users, products and a sample debt so you can try the system straight away."

    def handle(self, *args, **options):
        cs = CompanySettings.get()
        if not cs.boss_email:
            cs.boss_email = "boss@example.com"
            cs.boss_whatsapp = "237679910139"
            cs.save()

        boss, created = User.objects.get_or_create(username="boss", defaults={
            "role": User.OWNER, "first_name": "Boss", "is_staff": True, "is_superuser": True,
        })
        if created:
            boss.set_password("boss12345")
            boss.save()
        cashier, created = User.objects.get_or_create(username="cashier", defaults={
            "role": User.CASHIER, "first_name": "Cashier",
        })
        if created:
            cashier.set_password("cashier12345")
            cashier.save()

        for name, sku, cat, cost, price, stock, reorder in PRODUCTS:
            category, _ = Category.objects.get_or_create(name=cat)
            product, made = Product.objects.get_or_create(sku=sku, defaults={
                "name": name, "category": category, "cost_price": cost,
                "selling_price": price, "reorder_level": reorder,
            })
            if made:
                StockMovement.objects.create(product=product, kind=StockMovement.OPENING,
                                             quantity=stock, note="Opening stock", created_by=boss)

        if not Customer.objects.exists():
            today = timezone.localdate()
            amadou = Customer.objects.create(
                name="Amadou Garage", phone="677000111", address="Bonaberi, Douala",
                guarantor_name="Paul Ndi", guarantor_phone="699000222", credit_limit=300000,
            )
            debt = Debt.objects.create(
                customer=amadou, description="Brake pads x4 and engine oil", total=120000,
                issued_on=today - timedelta(days=20), due_date=today - timedelta(days=5), created_by=boss,
            )
            DebtPayment.objects.create(debt=debt, amount=40000, method="mtn_momo", reference="MP2610...",
                                       paid_on=today - timedelta(days=10), received_by=boss)

        self.stdout.write(self.style.SUCCESS(
            "Demo data ready.\n  Boss login:    boss / boss12345\n  Cashier login: cashier / cashier12345"
        ))
