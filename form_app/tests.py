from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from admin_app.serializers import AdminUserUpdateSerializer
from form_app.helpers import apply_manual_remaining_used_adjustment
from form_app.policies import get_leave_limits_for_employee
from form_app.policies import get_leave_year_range_for_employee
from form_app.models import LeaveRequest
from user_app.admin import EmployeeAdminForm
from user_app.helpers import _build_leave_year_context
from user_app.models import Employee


class ManualRemainingBalanceTests(TestCase):
    def setUp(self):
        self.year_start = date(2026, 4, 3)
        self.user = User.objects.create_user(username="balance-user")

    def _employee_with_manual_remaining(
        self,
        *,
        leave_type: str = "VACATION",
        yearly_limit: float = 16.0,
        remaining: float,
        snapshot_used: float | None,
        manual_used: float | None = None,
    ) -> Employee:
        overrides = {
            leave_type: yearly_limit,
            "_manual_remaining_by_year": {
                self.year_start.isoformat(): {leave_type: remaining}
            },
        }
        if snapshot_used is not None:
            overrides["_manual_remaining_used_snapshot_by_year"] = {
                self.year_start.isoformat(): {leave_type: snapshot_used}
            }
        if manual_used is not None:
            overrides["_manual_used_by_year"] = {
                self.year_start.isoformat(): {leave_type: manual_used}
            }

        return Employee.objects.create(
            user=self.user,
            joining_date=date(2025, 1, 1),
            probation_end_date=date(2025, 3, 31),
            leave_renewal_date_override=self.year_start,
            leave_limits_override=overrides,
        )

    def test_manual_remaining_target_deducts_new_approved_usage(self):
        employee = self._employee_with_manual_remaining(
            remaining=16.0, snapshot_used=0.0
        )

        used = apply_manual_remaining_used_adjustment(
            employee=employee,
            leave_year_start=self.year_start,
            leave_type="VACATION",
            yearly_limit=16.0,
            current_used=2.0,
        )

        self.assertEqual(used, 2.0)

    def test_admin_remaining_field_shows_adjusted_manual_target(self):
        employee = self._employee_with_manual_remaining(
            remaining=16.0, snapshot_used=0.0
        )
        LeaveRequest.objects.create(
            employee=employee,
            leave_type="VACATION",
            start_date=date(2026, 4, 30),
            end_date=date(2026, 5, 1),
            session="FULL",
            start_session="FULL",
            end_session="FULL",
            status=LeaveRequest.STATUS_APPROVED,
            is_paid=True,
        )

        form = EmployeeAdminForm()
        remaining = form._compute_remaining_for_type(
            emp=employee,
            leave_type="VACATION",
            year_start=self.year_start,
            manual_used={},
        )

        self.assertEqual(remaining, 14.0)

    def test_manual_remaining_without_snapshot_uses_implied_snapshot(self):
        employee = self._employee_with_manual_remaining(
            remaining=16.0, snapshot_used=None
        )

        used = apply_manual_remaining_used_adjustment(
            employee=employee,
            leave_year_start=self.year_start,
            leave_type="VACATION",
            yearly_limit=16.0,
            current_used=2.0,
        )

        self.assertEqual(used, 2.0)

    def test_legacy_sick_remaining_deducts_after_approval(self):
        employee = self._employee_with_manual_remaining(
            leave_type="SICK",
            yearly_limit=12.0,
            remaining=8.5,
            snapshot_used=None,
            manual_used=3.5,
        )
        LeaveRequest.objects.create(
            employee=employee,
            leave_type="SICK",
            start_date=date(2026, 4, 24),
            end_date=date(2026, 4, 24),
            session="FULL",
            start_session="FULL",
            end_session="FULL",
            status=LeaveRequest.STATUS_APPROVED,
            is_paid=True,
        )

        form = EmployeeAdminForm()
        remaining = form._compute_remaining_for_type(
            emp=employee,
            leave_type="SICK",
            year_start=self.year_start,
            manual_used={"SICK": 3.5},
        )

        self.assertEqual(remaining, 7.5)

    def test_admin_remaining_above_entitlement_raises_effective_limit(self):
        today = timezone.localdate()
        anchor_day = min(today.day, 28)
        renewal_anchor = date(2000, today.month, anchor_day)
        employee = Employee.objects.create(
            user=self.user,
            joining_date=date(2024, 1, 1),
            probation_end_date=date(2024, 3, 31),
            leave_renewal_date_override=renewal_anchor,
        )

        initial_form = EmployeeAdminForm(instance=employee)
        data = {}
        for name, field in initial_form.fields.items():
            value = initial_form.initial.get(name, field.initial)
            if name == "user":
                value = employee.user_id
            elif value is None:
                value = ""
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            data[name] = value
        data["remaining_vacation"] = "28"

        form = EmployeeAdminForm(data=data, instance=employee)
        self.assertTrue(form.is_valid(), form.errors.as_data())
        form.save()

        employee.refresh_from_db()
        self.assertEqual(
            get_leave_limits_for_employee(employee).get("VACATION"),
            28.0,
        )


class AdminUserUpdateLeaveOverrideTests(TestCase):
    def test_camel_case_leave_limit_override_updates_employee(self):
        user = User.objects.create_user(username="camel-user")
        employee = Employee.objects.create(
            user=user,
            joining_date=date(2024, 1, 1),
            probation_end_date=date(2024, 3, 31),
        )

        serializer = AdminUserUpdateSerializer(
            data={
                "user_id": user.id,
                "leaveLimitsOverride": {"VACATION": 28},
                "applyLeaveLimitsOverride": True,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.update_user()

        employee.refresh_from_db()
        self.assertEqual(employee.leave_limits_override.get("VACATION"), 28.0)


class EmployeeDashboardLeaveYearTests(TestCase):
    def test_dashboard_stays_on_current_leave_year_with_future_approved_leave(self):
        user = User.objects.create_user(username="future-leave-user")
        employee = Employee.objects.create(
            user=user,
            joining_date=date(2024, 1, 1),
            probation_end_date=date(2024, 3, 31),
            leave_renewal_date_override=date(2000, 5, 18),
        )
        current_start, current_end_excl = get_leave_year_range_for_employee(
            employee, on_date=timezone.localdate()
        )
        LeaveRequest.objects.create(
            employee=employee,
            leave_type="VACATION",
            start_date=current_end_excl,
            end_date=current_end_excl,
            session="FULL",
            start_session="FULL",
            end_session="FULL",
            status=LeaveRequest.STATUS_APPROVED,
            is_paid=True,
        )

        ctx = _build_leave_year_context(employee)

        self.assertEqual(ctx.leave_year_start, current_start)
        self.assertEqual(ctx.leave_year_end_excl, current_end_excl)
