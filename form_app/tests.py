from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase

from form_app.helpers import apply_manual_remaining_used_adjustment
from form_app.models import LeaveRequest
from user_app.admin import EmployeeAdminForm
from user_app.models import Employee


class ManualRemainingBalanceTests(TestCase):
    def setUp(self):
        self.year_start = date(2026, 4, 3)
        self.user = User.objects.create_user(username="balance-user")

    def _employee_with_manual_remaining(
        self, *, remaining: float, snapshot_used: float | None
    ) -> Employee:
        overrides = {
            "VACATION": 16.0,
            "_manual_remaining_by_year": {
                self.year_start.isoformat(): {"VACATION": remaining}
            },
        }
        if snapshot_used is not None:
            overrides["_manual_remaining_used_snapshot_by_year"] = {
                self.year_start.isoformat(): {"VACATION": snapshot_used}
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

    def test_manual_remaining_without_snapshot_keeps_legacy_behavior(self):
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

        self.assertEqual(used, 0.0)
