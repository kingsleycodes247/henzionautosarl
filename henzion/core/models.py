from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class User(AbstractUser):
    OWNER = "owner"
    CASHIER = "cashier"
    ROLE_CHOICES = [(OWNER, "Owner / Boss"), (CASHIER, "Cashier")]

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=CASHIER)

    @property
    def is_owner(self):
        return self.role == self.OWNER or self.is_superuser

    def display_name(self):
        return self.get_full_name() or self.username


class CompanySettings(models.Model):
    """Single-row table holding the company identity and business rules."""

    name = models.CharField(max_length=120, default="HenZion Auto SARL")
    tagline = models.CharField(max_length=160, default="Automotive Spare Parts & Fluids")
    description = models.CharField(
        max_length=255,
        default="Dealer in automobile spare parts, engine oil, transmission fluid, batteries of all sizes, etc.",
    )
    address = models.CharField(max_length=200, default="Pont de Camp Yabassi 6ème")
    phones = models.CharField(max_length=120, default="+237 673 441 221 / +237 679 910 139")
    niu = models.CharField("NIU / Tax ID", max_length=40, default="M052217397952Y")

    boss_email = models.EmailField(blank=True, help_text="Receives discount approvals and sales reports.")
    boss_whatsapp = models.CharField(
        max_length=20, blank=True, help_text="With country code, e.g. 237679910139."
    )

    vat_rate = models.DecimalField(
        "VAT rate (%)", max_digits=5, decimal_places=2, default=0,
        help_text="0 = no VAT on invoices. Cameroon standard rate is 19.25.",
    )
    auto_approve_percent = models.PositiveIntegerField(
        "Auto-approve discounts up to (%)", default=0,
        help_text="Discounts at or below this percentage don't need the boss. 0 = always ask.",
    )
    approval_timeout_minutes = models.PositiveIntegerField(
        "Approval timeout (minutes)", default=10,
        help_text="If the boss doesn't answer in time, the original price stays.",
    )

    eod_start_hour = models.PositiveSmallIntegerField(
        "End-of-day report starts at (hour, 0-23)", default=18, validators=[MaxValueValidator(23)],
        help_text="From this hour the dashboard offers to email today's sales report. 18 = 6pm.",
    )
    eod_end_hour = models.PositiveSmallIntegerField(
        "End-of-day window ends at (hour, 1-24)", default=20, validators=[MinValueValidator(1), MaxValueValidator(24)],
        help_text="A cashier logging out between the two hours triggers the report automatically. 20 = 8pm.",
    )
    eod_auto_on_logout = models.BooleanField(
        "Email the report automatically when a cashier logs out in that window", default=True,
    )

    class Meta:
        verbose_name_plural = "Company settings"

    def __str__(self):
        return self.name

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AuditLog(models.Model):
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action}: {self.target}"


def log(user, action, target, details=""):
    AuditLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action,
        target=str(target)[:200],
        details=details,
    )
