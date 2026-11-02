"""Rules for removing or disabling user accounts without damaging the shop's records."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q


def other_active_owners(user):
    """How many other boss accounts can still log in."""
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(Q(role=User.OWNER) | Q(is_superuser=True)).exclude(pk=user.pk).count()


def why_cannot_remove(user, acting_user):
    """A sentence saying why this account may not be disabled or deleted, or '' if it may."""
    if user.pk == acting_user.pk:
        return "You can't disable or delete your own account while you are logged in with it."
    if user.is_owner and user.is_active and other_active_owners(user) == 0:
        return "This is the only active boss account. Create or enable another boss first."
    return ""


def records_of(user):
    from debts.models import DebtPayment
    from sales.models import Sale

    return {"sales": Sale.objects.filter(created_by=user).count(), "payments": DebtPayment.objects.filter(received_by=user).count()}


@transaction.atomic
def delete_user_keeping_records(user):
    """Deletes the account. Sales, invoices, payments and stock history stay; the user's name is
    first written onto the records that show it, so 'by Marie' still reads 'by Marie' afterwards."""
    from debts.models import DebtPayment
    from sales.models import Sale

    name = user.display_name()
    Sale.objects.filter(created_by=user, created_by_name="").update(created_by_name=name)
    DebtPayment.objects.filter(received_by=user, received_by_name="").update(received_by_name=name)
    user.delete()
