from django.urls import path

from . import views

urlpatterns = [
    path("", views.report_view, name="reports"),
    path("eod/send/", views.eod_send, name="eod_send"),
]
