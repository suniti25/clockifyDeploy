from datetime import timedelta

from form_app.policies import get_probation_days

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.html import format_html

from .models import Employee, Profile


class UserCreationFormWithRole(UserCreationForm):
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        initial=Profile.ROLE_EMPLOYEE,
        required=True,
        help_text="Admin = full access to Django admin; Employee = default access.",
    )

    def save(self, commit=True):
        user = super().save(commit=False)
        role = self.cleaned_data.get("role")

        if role == Profile.ROLE_ADMIN:
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False

        if commit:
            user.save()
        return user


class UserAdmin(DjangoUserAdmin):
    add_form = UserCreationFormWithRole
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "password1", "password2", "role"),
            },
        ),
    )


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass

admin.site.register(User, UserAdmin)


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
