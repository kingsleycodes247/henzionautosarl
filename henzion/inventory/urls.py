from django.urls import path

from . import views

urlpatterns = [
    path("", views.ProductListView.as_view(), name="product_list"),
    path("new/", views.ProductCreateView.as_view(), name="product_create"),
    path("<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_update"),
    path("<int:pk>/delete/", views.ProductDeleteView.as_view(), name="product_delete"),
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("movements/", views.MovementListView.as_view(), name="movement_list"),
    path("valuation/", views.stock_value, name="stock_value"),
    path("receive/", views.receive_stock, name="receive_stock"),
    path("adjust/", views.adjust_stock, name="adjust_stock"),
]
