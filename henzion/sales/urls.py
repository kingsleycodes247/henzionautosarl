from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.pos, name="pos"),
    path("", views.SaleListView.as_view(), name="sale_list"),
    path("unpaid/", views.proforma_list, name="proforma_list"),
    path("unpaid/new/", views.proforma_create, name="proforma_create"),
    path("unpaid/<int:pk>/", views.proforma_detail, name="proforma_detail"),
    path("unpaid/<int:pk>/invoice/", views.proforma_invoice, name="proforma_invoice"),
    path("unpaid/<int:pk>/prices/", views.proforma_toggle_prices, name="proforma_toggle_prices"),
    path("unpaid/<int:pk>/cancel/", views.proforma_cancel, name="proforma_cancel"),
    path("<int:pk>/", views.sale_detail, name="sale_detail"),
    path("<int:pk>/invoice/", views.invoice, name="invoice"),
    path("<int:pk>/cancel/", views.sale_cancel, name="sale_cancel"),
    path("discount/request/", views.discount_request, name="discount_request"),
    path("discount/<int:pk>/status/", views.discount_status, name="discount_status"),
    path("discount/decide/<str:token>/", views.discount_decide, name="discount_decide"),
    path("neighbors/", views.neighbor_items, name="neighbor_items"),
    path("neighbors/item/<int:pk>/edit/", views.NeighborItemUpdateView.as_view(), name="neighbor_item_update"),
    path("neighbors/item/<int:pk>/paid/", views.neighbor_item_paid, name="neighbor_item_paid"),
    path("neighbors/<int:pk>/edit/", views.NeighborUpdateView.as_view(), name="neighbor_update"),
    path("neighbors/<int:pk>/settle/", views.neighbor_settle, name="neighbor_settle"),
    path("discounts/", views.DiscountListView.as_view(), name="discount_list"),
    path("discounts/<int:pk>/decide/", views.discount_decide_dashboard, name="discount_decide_dashboard"),
]
