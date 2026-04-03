from datetime import timedelta

from django.utils import timezone
from form_app.policies import compute_probation_end_date
from form_app.policies import get_leave_limits_for_employee
from form_app.policies import get_probation_days
from form_app.policies import get_leave_year_range_for_employee

from form_app.helpers import (
    get_manual_topup_by_type,
    get_manual_used_by_type,
    overlapping_days,
)
from form_app.models import LeaveRequest

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
    remaining_vacation = forms.FloatField(required=False, min_value=0)
    remaining_sick = forms.FloatField(required=False, min_value=0)
    remaining_maternity = forms.FloatField(required=False, min_value=0)
    remaining_paternity = forms.FloatField(required=False, min_value=0)
    remaining_bereavement = forms.FloatField(required=False, min_value=0)
    manual_used_vacation = forms.FloatField(required=False, min_value=0)
    manual_used_sick = forms.FloatField(required=False, min_value=0)
    manual_used_maternity = forms.FloatField(required=False, min_value=0)
    manual_used_paternity = forms.FloatField(required=False, min_value=0)
    manual_used_bereavement = forms.FloatField(required=False, min_value=0)

    class Meta:
        model = Employee
        fields = "__all__"

    _FIELD_TO_TYPE = {
        "remaining_vacation": "VACATION",
        "remaining_sick": "SICK",
        "remaining_maternity": "MATERNITY",
        "remaining_paternity": "PATERNITY",
        "remaining_bereavement": "BEREAVEMENT",
    }

    _MANUAL_FIELD_TO_TYPE = {
        "manual_used_vacation": "VACATION",
        "manual_used_sick": "SICK",
        "manual_used_maternity": "MATERNITY",
        "manual_used_paternity": "PATERNITY",
        "manual_used_bereavement": "BEREAVEMENT",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._manual_year_start = None
        self._manual_used_initial: dict[str, float] = {}
        self._manual_topup_initial: dict[str, float] = {}

        emp = getattr(self, "instance", None)
        if not emp or not getattr(emp, "pk", None):
            return

        year_start, _year_end_excl = get_leave_year_range_for_employee(
            emp, on_date=timezone.localdate()
        )
        self._manual_year_start = year_start

        manual_used = get_manual_used_by_type(employee=emp, leave_year_start=year_start)
        manual_topup = get_manual_topup_by_type(
            employee=emp, leave_year_start=year_start
        )
        self._manual_used_initial = dict(manual_used)
        self._manual_topup_initial = dict(manual_topup)
        for field_name, lt in self._MANUAL_FIELD_TO_TYPE.items():
            if lt in manual_used:
                self.fields[field_name].initial = manual_used.get(lt)

        for field_name, lt in self._FIELD_TO_TYPE.items():
            remaining = self._compute_remaining_for_type(
                emp=emp,
                leave_type=lt,
                year_start=year_start,
                manual_used=manual_used,
                manual_topup=manual_topup,
            )
            self.fields[field_name].initial = remaining

        # Make intent clear in the UI.
        remaining_help = (
            f"Target remaining balance for the current leave year starting "
            f"{year_start.isoformat()}. Use 0.5 increments."
        )
        for field_name in self._FIELD_TO_TYPE.keys():
            self.fields[field_name].help_text = remaining_help

        manual_help = (
            f"Edits apply to current leave year starting {year_start.isoformat()}. "
            "Use 0.5 increments (e.g. 1.5). Leave blank to clear."
        )
        for field_name in self._MANUAL_FIELD_TO_TYPE.keys():
            self.fields[field_name].help_text = manual_help

    def _carry_forward_for_year(self, emp: Employee, year_start):
        if not year_start:
            return 0.0
        from user_app.helpers import carry_forward_only

        prev_day = year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(
            emp, on_date=prev_day
        )
        return float(carry_forward_only(emp, prev_start, prev_end_excl))

    def _approved_paid_used_for_type(
        self, *, emp: Employee, leave_type: str, year_start
    ):
        _year_start, year_end_excl = get_leave_year_range_for_employee(
            emp, on_date=year_start
        )
        qs = LeaveRequest.objects.filter(
            employee=emp,
            leave_type__iexact=leave_type,
            status=LeaveRequest.STATUS_APPROVED,
            is_paid=True,
            start_date__lt=year_end_excl,
            end_date__gte=year_start,
        )
        return float(sum(overlapping_days(lr, year_start, year_end_excl) for lr in qs))

    def _compute_total_allowed_for_type(
        self, *, emp: Employee, leave_type: str, year_start
    ):
        limits = get_leave_limits_for_employee(emp)
        total_allowed = float(limits.get(leave_type, 0.0))
        if leave_type == "VACATION":
            total_allowed += float(self._carry_forward_for_year(emp, year_start))
        return float(total_allowed)

    def _compute_remaining_for_type(
        self,
        *,
        emp: Employee,
        leave_type: str,
        year_start,
        manual_used=None,
        manual_topup=None,
    ):
        manual_used = manual_used or {}
        manual_topup = manual_topup or {}
        total_allowed = self._compute_total_allowed_for_type(
            emp=emp, leave_type=leave_type, year_start=year_start
        )
        approved_used = self._approved_paid_used_for_type(
            emp=emp, leave_type=leave_type, year_start=year_start
        )
        current_manual = float(manual_used.get(leave_type, 0.0) or 0.0)
        current_topup = float(manual_topup.get(leave_type, 0.0) or 0.0)
        return _round_to_half_day(
            max(total_allowed + current_topup - approved_used - current_manual, 0.0)
        )

    def clean(self):
        cleaned = super().clean()

        joining_date = cleaned.get("joining_date")
        probation_end_date = cleaned.get("probation_end_date")

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

        for field_name in list(self._FIELD_TO_TYPE.keys()) + list(
            self._MANUAL_FIELD_TO_TYPE.keys()
        ):
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
            topup_by_year = merged.get("_manual_topup_by_year")
            if not isinstance(topup_by_year, dict):
                topup_by_year = {}
            topup_by_year = dict(topup_by_year)

            key = year_start.isoformat()
            year_map: dict[str, float] = {}
            topup_map: dict[str, float] = {}

            for field_name, lt in self._MANUAL_FIELD_TO_TYPE.items():
                v = self.cleaned_data.get(field_name)
                if v is None:
                    continue
                year_map[lt] = float(v)

            changed_remaining_types = {
                lt
                for field_name, lt in self._FIELD_TO_TYPE.items()
                if field_name in self.changed_data
            }
            changed_manual_types = {
                lt
                for field_name, lt in self._MANUAL_FIELD_TO_TYPE.items()
                if field_name in self.changed_data
            }

            for field_name, lt in self._FIELD_TO_TYPE.items():
                desired_remaining = self.cleaned_data.get(field_name)
                if desired_remaining is None:
                    continue

                total_allowed = self._compute_total_allowed_for_type(
                    emp=emp, leave_type=lt, year_start=year_start
                )
                approved_used = self._approved_paid_used_for_type(
                    emp=emp, leave_type=lt, year_start=year_start
                )

                if lt in changed_remaining_types:
                    # Remaining balance is the source of truth for this leave type.
                    delta = float(desired_remaining) - float(
                        total_allowed - approved_used
                    )
                    if delta >= 0.0:
                        topup_map[lt] = float(_round_to_half_day(delta))
                        year_map[lt] = 0.0
                    else:
                        topup_map[lt] = 0.0
                        year_map[lt] = float(_round_to_half_day(abs(delta)))
                    continue

                if lt in changed_manual_types:
                    base_manual = float(year_map.get(lt, 0.0) or 0.0)
                else:
                    base_manual = float(self._manual_used_initial.get(lt, 0.0) or 0.0)

                topup_map[lt] = float(self._manual_topup_initial.get(lt, 0.0) or 0.0)
                year_map.setdefault(lt, base_manual)

            if year_map:
                by_year[key] = year_map
            else:
                # If all fields are blank, clear the manual-used map for this year.
                by_year.pop(key, None)

            topup_map = {
                lt: float(v) for lt, v in topup_map.items() if float(v or 0.0) > 0.0
            }
            if topup_map:
                topup_by_year[key] = topup_map
            else:
                topup_by_year.pop(key, None)

            if by_year:
                merged["_manual_used_by_year"] = by_year
            else:
                merged.pop("_manual_used_by_year", None)

            if topup_by_year:
                merged["_manual_topup_by_year"] = topup_by_year
            else:
                merged.pop("_manual_topup_by_year", None)

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
            "Remaining Balance (Current Leave Year)",
            {
                "fields": (
                    "remaining_vacation",
                    "remaining_sick",
                    "remaining_maternity",
                    "remaining_paternity",
                    "remaining_bereavement",
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
