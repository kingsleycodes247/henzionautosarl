from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import pwa_views

admin.site.site_header = "HenZion Auto SARL"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("manifest.webmanifest", pwa_views.manifest, name="pwa_manifest"),
    path("sw.js", pwa_views.service_worker, name="pwa_sw"),
    path("offline/", pwa_views.offline, name="pwa_offline"),
    path("push/status/", pwa_views.push_status, name="push_status"),
    path("push/subscribe/", pwa_views.push_subscribe, name="push_subscribe"),
    path("push/unsubscribe/", pwa_views.push_unsubscribe, name="push_unsubscribe"),
    path("push/test/", pwa_views.push_test, name="push_test"),
    path("", include("core.urls")),
    path("inventory/", include("inventory.urls")),
    path("sales/", include("sales.urls")),
    path("debts/", include("debts.urls")),
    path("reports/", include("reports.urls")),
]
