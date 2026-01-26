# form_app/helpers.py

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple

from django.utils.timezone import localdate
from rest_framework import serializers

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest

from user_app.helpers import get_leave_year_range, carry_forward_only


def validate_leave_application_inputs(
    *,
    profile,
    employee,
    start_date,
    end_date,
    leave_type: str,
    session: str,
    reason: str,
    instance_id: Optional[int] = None,
):
    """
    Raises serializers.ValidationError if invalid.
    Keeps serializer clean + reusable from other endpoints.
    """
    if not profile:
        raise serializers.ValidationError("User profile not found.")

    if profile.role == "ADMIN":
        raise serializers.ValidationError("Admins cannot apply for leave.")

    if not employee:
        raise serializers.ValidationError("Employee record not found.")

    today = localdate()

    if start_date is None or end_date is None:
        raise serializers.ValidationError("Start date and end date are required.")

    if start_date < today:
        raise serializers.ValidationError("You cannot apply leave with a start date in the past.")

    if end_date < start_date:
        raise serializers.ValidationError("End date cannot be earlier than start date.")

    if session in ["AM", "PM"] and start_date != end_date:
        raise serializers.ValidationError("AM/PM session can only be applied for a single day.")

    if leave_type in ["SICK", "WFH"] and not (reason or "").strip():
        raise serializers.ValidationError("Reason is required for SICK and WFH leave.")

    # Overlapping leave check (pending + approved)
    overlapping = LeaveRequest.objects.filter(
        employee=employee,
        start_date__lte=end_date,
        end_date__gte=start_date,
        status__in=["PENDING", "APPROVED"],
    )

    if instance_id:
        overlapping = overlapping.exclude(id=instance_id)

    if overlapping.exists():
        raise serializers.ValidationError("You already have a leave applied for this date range.")


def compute_leave_days_for_payload(*, employee, payload: dict) -> float:
    """
    Uses model total_days() for consistent logic.
    """
    temp = LeaveRequest(employee=employee, **payload)
    return temp.total_days()


def compute_paid_status(
    *,
    employee,
    leave_type: str,
    leave_days: float,
    start_date,
    instance_id: Optional[int] = None,
) -> bool:
    """
    Paid/unpaid logic MUST match dashboard:
    - probation => unpaid
    - leave year anchored to probation_end_date
    - carry forward only VACATION (carry_forward_only)
    - count used PAID approved in same window
    """
    # probation => always unpaid
    if employee.is_on_probation():
        return False

    # If not limited, consider paid (keeps your earlier behavior)
    if leave_type not in LEAVE_LIMITS:
        return True

    leave_year_start, leave_year_end_excl = get_leave_year_range(employee.probation_end_date, start_date)
    leave_year_end_incl = leave_year_end_excl - timedelta(days=1)

    vacation_carry = 0
    if leave_type == "VACATION":
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range(employee.probation_end_date, prev_day)
        vacation_carry = carry_forward_only(employee, prev_start, prev_end_excl)

    qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=leave_type,
        status="APPROVED",
        is_paid=True,
        start_date__lte=leave_year_end_incl,
        end_date__gte=leave_year_start,
    )

    if instance_id:
        qs = qs.exclude(id=instance_id)

    used = sum(lr.total_days() for lr in qs)

    total_allowed = LEAVE_LIMITS[leave_type] + vacation_carry
    remaining = max(total_allowed - used, 0)

    # boolean (no partial paid support)
    return leave_days <= remaining
