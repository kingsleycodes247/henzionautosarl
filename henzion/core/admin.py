from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuditLog, CompanySettings, User


@admin.register(User)
class HenzionUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Role", {"fields": ("role",)}),)
    list_display = ("username", "first_name", "last_name", "role", "is_active")


admin.site.register(CompanySettings)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target")
    readonly_fields = ("user", "action", "target", "details", "created_at")
