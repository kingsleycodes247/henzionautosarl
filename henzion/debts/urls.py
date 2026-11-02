from django.urls import path

from . import views

urlpatterns = [
    path("", views.DebtListView.as_view(), name="debt_list"),
    path("new/", views.DebtCreateView.as_view(), name="debt_create"),
    path("<int:pk>/", views.DebtDetailView.as_view(), name="debt_detail"),
    path("<int:pk>/edit/", views.DebtUpdateView.as_view(), name="debt_update"),
    path("<int:pk>/delete/", views.DebtDeleteView.as_view(), name="debt_delete"),
    path("<int:pk>/pay/", views.payment_create, name="payment_create"),
    path("payments/<int:pk>/edit/", views.PaymentUpdateView.as_view(), name="payment_update"),
    path("payments/<int:pk>/delete/", views.PaymentDeleteView.as_view(), name="payment_delete"),
    path("payments/<int:pk>/receipt/", views.payment_receipt, name="payment_receipt"),
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/new/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<int:pk>/", views.CustomerDetailView.as_view(), name="customer_detail"),
    path("customers/<int:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_update"),
    path("customers/<int:pk>/delete/", views.CustomerDeleteView.as_view(), name="customer_delete"),
]
