from datetime import timedelta

from django.utils import timezone
from form_app.policies import compute_probation_end_date
from form_app.policies import get_probation_days
from form_app.policies import get_leave_year_range_for_employee

from form_app.helpers import get_manual_used_by_type

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.html import format_html

from .models import Employee, Profile


def _round_to_half_day(value: float) -> float:
    # Keep consistent with the rest of the app: half-day granularity.
    return round(float(value) * 2.0) / 2.0


class EmployeeAdminForm(forms.ModelForm):
    manual_used_vacation = forms.FloatField(required=False, min_value=0)
    manual_used_sick = forms.FloatField(required=False, min_value=0)
    manual_used_maternity = forms.FloatField(required=False, min_value=0)
    manual_used_paternity = forms.FloatField(required=False, min_value=0)
    manual_used_bereavement = forms.FloatField(required=False, min_value=0)

    class Meta:
        model = Employee
        fields = "__all__"

    _FIELD_TO_TYPE = {
        "manual_used_vacation": "VACATION",
        "manual_used_sick": "SICK",
        "manual_used_maternity": "MATERNITY",
        "manual_used_paternity": "PATERNITY",
        "manual_used_bereavement": "BEREAVEMENT",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._manual_year_start = None

        emp = getattr(self, "instance", None)
        if not emp or not getattr(emp, "pk", None):
            return

        year_start, _year_end_excl = get_leave_year_range_for_employee(
            emp, on_date=timezone.localdate()
        )
        self._manual_year_start = year_start

        manual_used = get_manual_used_by_type(employee=emp, leave_year_start=year_start)
        for field_name, lt in self._FIELD_TO_TYPE.items():
            if lt in manual_used:
                self.fields[field_name].initial = manual_used.get(lt)

        # Make intent clear in the UI.
        help_suffix = (
            f"Edits apply to current leave year starting {year_start.isoformat()}. "
            "Use 0.5 increments (e.g. 1.5). Leave blank to clear."
        )
        for field_name in self._FIELD_TO_TYPE.keys():
            self.fields[field_name].help_text = help_suffix

    def clean(self):
        cleaned = super().clean()

        joining_date = cleaned.get("joining_date")
        probation_end_date = cleaned.get("probation_end_date")

        # Keep probation_end_date in sync with joining_date by default.
        # - On create: auto-fill if left blank.
        # - On edit: if joining_date changed and user didn't edit probation_end_date,
        #   recompute it.
        if joining_date:
            if probation_end_date is None:
                cleaned["probation_end_date"] = compute_probation_end_date(joining_date)
            else:
                initial_joining = self.initial.get("joining_date")
                initial_probation = self.initial.get("probation_end_date")
                if (
                    initial_joining
                    and joining_date != initial_joining
                    and probation_end_date == initial_probation
                ):
                    cleaned["probation_end_date"] = compute_probation_end_date(
                        joining_date
                    )

        probation_end_date = cleaned.get("probation_end_date")
        if joining_date and probation_end_date and probation_end_date < joining_date:
            self.add_error(
                "probation_end_date",
                "Probation end date cannot be before joining date.",
            )

        for field_name in self._FIELD_TO_TYPE.keys():
            v = cleaned.get(field_name)
            if v is None:
                continue
            cleaned[field_name] = _round_to_half_day(float(v))
        return cleaned

    def save(self, commit=True):
        emp: Employee = super().save(commit=False)

        if emp and getattr(emp, "pk", None):
            year_start = self._manual_year_start
            if not year_start:
                year_start, _ = get_leave_year_range_for_employee(
                    emp, on_date=timezone.localdate()
                )

            existing = emp.leave_limits_override
            if not isinstance(existing, dict):
                existing = {}
            merged = dict(existing)

            by_year = merged.get("_manual_used_by_year")
            if not isinstance(by_year, dict):
                by_year = {}
            by_year = dict(by_year)

            key = year_start.isoformat()
            year_map: dict[str, float] = {}
            for field_name, lt in self._FIELD_TO_TYPE.items():
                v = self.cleaned_data.get(field_name)
                if v is None:
                    continue
                year_map[lt] = float(v)

            if year_map:
                by_year[key] = year_map
            else:
                # If all fields are blank, clear the manual-used map for this year.
                by_year.pop(key, None)

            if by_year:
                merged["_manual_used_by_year"] = by_year
            else:
                merged.pop("_manual_used_by_year", None)

            emp.leave_limits_override = merged

        if commit:
            emp.save()
            self.save_m2m()
        return emp


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
    form = EmployeeAdminForm
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

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "user",
                    "name",
                    "joining_date",
                    "probation_end_date",
                    "current_project",
                    "leave_renewal_date_override",
                    "reset_leave_balance",
                )
            },
        ),
        (
            "Manual Used (Current Leave Year)",
            {
                "fields": (
                    "manual_used_vacation",
                    "manual_used_sick",
                    "manual_used_maternity",
                    "manual_used_paternity",
                    "manual_used_bereavement",
                )
            },
        ),
        (
            "Leave Overrides (Advanced)",
            {"fields": ("leave_limits_override",)},
        ),
    )

    def probation_check(self, obj):
        try:
            if not getattr(obj, "joining_date", None) or not getattr(
                obj, "probation_end_date", None
            ):
                return "N/A"

            days = int(get_probation_days())
            expected = (
                obj.joining_date - timedelta(days=1)
                if days <= 0
                else obj.joining_date + timedelta(days=days - 1)
            )
            if obj.probation_end_date != expected:
                return format_html("<b style='color:red;'>Mismatch</b>")
            return "OK"
        except Exception:
            # Never break Django admin list view.
            return "N/A"

    probation_check.short_description = "Probation (90d)"


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "role", "employee")
    search_fields = ("user__username", "user__email")
    list_filter = ("role",)
    list_select_related = ("user", "employee")
