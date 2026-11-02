CASH = "cash"
MTN = "mtn_momo"
ORANGE = "orange_money"
CREDIT = "credit"
SPLIT = "split"  # a sale paid with more than one method

# The four ways money can be recorded on a sale (one sale may mix them).
SALE_METHODS = [
    (CASH, "Cash"),
    (MTN, "MTN Mobile Money"),
    (ORANGE, "Orange Money"),
    (CREDIT, "Credit"),
]

# What a Sale row stores as its summary: one of the four, or "split".
SALE_METHOD_CHOICES = SALE_METHODS + [(SPLIT, "Split payment")]

# Ways a customer can pay money later (settling a debt).
PAYMENT_METHODS = [
    (CASH, "Cash"),
    (MTN, "MTN Mobile Money"),
    (ORANGE, "Orange Money"),
]

METHOD_LABELS = dict(SALE_METHOD_CHOICES)
