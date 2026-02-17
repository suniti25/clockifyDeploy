from datetime import timedelta

from form_app.policies import get_probation_days

from django.contrib import admin
from django.utils.html import format_html

from .models import Employee, Profile


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "name",
        "current_project",  
        "joining_date",
        "probation_end_date",
        "probation_check",
    )
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "name",
        "current_project",  
    )
    list_filter = ("joining_date", "probation_end_date")
    ordering = ("-joining_date",)
    list_select_related = ("user",)

    def probation_check(self, obj):
        expected = obj.joining_date + timedelta(days=get_probation_days())
        if obj.probation_end_date != expected:
            return format_html("<b style='color:red;'>Mismatch</b>")
        return "OK"

    probation_check.short_description = "Probation (90d)"


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "role", "employee")
    search_fields = ("user__username", "user__email")
    list_filter = ("role",)
    list_select_related = ("user", "employee")
