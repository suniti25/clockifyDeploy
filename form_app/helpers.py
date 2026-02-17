from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from django.db.models import CharField, F, Value
from django.db.models.functions import Replace, Trim, Upper
from django.utils.timezone import localdate
from rest_framework import serializers

from form_app.policies import (
    get_leave_limits,
    get_carryover_percentage,
    get_leave_year_range_for_employee,
)
from form_app.models import LeaveRequest

MAX_FUTURE_DAYS = 365
MAX_LEAVE_DAYS = 60


def _norm_status_expr(field_name: str = "status"):
    return Upper(
        Trim(
            Replace(
                F(field_name),
                Value("\u00A0"),
                Value(""),
                output_field=CharField(),
            )
        )
    )

# Normalizers / validators
def normalize_session(session: str | None) -> str:
    if session is None:
        raise serializers.ValidationError("Session is required (FD/FULL, AM, or PM).")

    s = str(session).strip().upper()
    if s == "FD":
        s = "FULL"

    if s not in {"FULL", "AM", "PM"}:
        raise serializers.ValidationError("Invalid session. Use FD/FULL, AM, or PM.")

    return s


def normalize_leave_type(leave_type: str | None) -> str:

    lt = (leave_type or "").strip().upper()
    if lt in {"WORK FROM HOME", "WORKFROMHOME"}:
        lt = "WFH"
    return lt


def display_is_paid(leave_type: str | None, is_paid: bool | None) -> Optional[bool]:
    if normalize_leave_type(leave_type) == "WFH":
        return None
    return bool(is_paid)


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
    allow_overlap_with_id: Optional[int] = None, 
) -> Tuple[str, str]:
    
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

    if start_date > today + timedelta(days=MAX_FUTURE_DAYS):
        raise serializers.ValidationError(
            f"You cannot apply leave more than {MAX_FUTURE_DAYS} days in the future."
        )

    if end_date < start_date:
        raise serializers.ValidationError("End date cannot be earlier than start date.")

    total_days = (end_date - start_date).days + 1
    if total_days > MAX_LEAVE_DAYS:
        raise serializers.ValidationError(f"Leave duration cannot exceed {MAX_LEAVE_DAYS} days.")

    normalized_session = normalize_session(session)
    normalized_leave_type = normalize_leave_type(leave_type)

    #  the request will be rejected since if there is no working day apply for leave on weekend-only days.
    tmp = LeaveRequest(
        employee=employee,
        leave_type=normalized_leave_type,
        start_date=start_date,
        end_date=end_date,
        session=normalized_session,
        start_session=normalized_session,
        end_session=normalized_session,
        status=LeaveRequest.STATUS_PENDING,
    )
    if float(tmp.total_days()) <= 0.0:
        raise serializers.ValidationError(
            "Selected date range has 0 working days (weekend-only). Please choose weekdays."
        )

    if normalized_leave_type in {"SICK", "WFH"} and not (reason or "").strip():
        raise serializers.ValidationError("Reason is required for SICK and WFH leave.")

    overlapping = (
        LeaveRequest.objects.annotate(_status_norm=_norm_status_expr("status"))
        .filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            _status_norm__in=[LeaveRequest.STATUS_PENDING, LeaveRequest.STATUS_APPROVED],
        )
    )

    if instance_id:
        overlapping = overlapping.exclude(id=instance_id)

    if allow_overlap_with_id:
        overlapping = overlapping.exclude(id=allow_overlap_with_id)

    if overlapping.exists():
        raise serializers.ValidationError("You already have a leave applied for this date range.")

    return normalized_session, normalized_leave_type


def compute_leave_days_for_payload(*, employee, payload: dict) -> float:
    temp = LeaveRequest(employee=employee, **payload)
    return float(temp.total_days())

def _round_to_half_day(value: float) -> float:
    d = Decimal(str(value))
    half = Decimal("0.5")
    return float((d / half).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * half)


def overlapping_days(req: LeaveRequest, window_start: date, window_end_exclusive: date) -> float:
    overlap_start = max(req.start_date, window_start)
    overlap_end = min(req.end_date, window_end_exclusive - timedelta(days=1))

    if overlap_start > overlap_end:
        return 0.0

    # Count only weekdays (Mon-Fri), consistent with LeaveRequest.total_days()
    days_int = 0
    cur = overlap_start
    while cur <= overlap_end:
        if cur.weekday() < 5:
            days_int += 1
        cur += timedelta(days=1)

    session = (getattr(req, "session", "FULL") or "FULL").strip().upper()
    if session == "FD":
        session = "FULL"
    if session in {"AM", "PM"}:
        return max(float(days_int) * 0.5, 0.0)

    if req.start_date == req.end_date:
        return 1.0 if req.start_date.weekday() < 5 else 0.0

    start_sess = (getattr(req, "start_session", "FULL") or "FULL").strip().upper()
    end_sess = (getattr(req, "end_session", "FULL") or "FULL").strip().upper()
    if start_sess == "FD":
        start_sess = "FULL"
    if end_sess == "FD":
        end_sess = "FULL"

    days = float(days_int)
    if overlap_start == req.start_date and start_sess == "PM" and overlap_start.weekday() < 5:
        days -= 0.5
    if overlap_end == req.end_date and end_sess == "AM" and overlap_end.weekday() < 5:
        days -= 0.5

    return max(days, 0.0)


def carry_forward_only(employee, prev_start: date, prev_end_exclusive: date) -> float:
    if bool(getattr(employee, "reset_leave_balance", False)):
        return 0.0

    limits = get_leave_limits()
    vacation_limit = float(limits.get("VACATION", 0.0))

    qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type="VACATION",
        status="APPROVED",
        is_paid=True,
        start_date__lt=prev_end_exclusive,
        end_date__gte=prev_start,
    )

    used = sum(overlapping_days(lr, prev_start, prev_end_exclusive) for lr in qs)
    remaining = max(vacation_limit - float(used), 0.0)

    carry_pct = max(0, min(get_carryover_percentage(), 100))
    carry_raw = remaining * (float(carry_pct) / 100.0)
    carry = _round_to_half_day(carry_raw)
    carry_floor = int(carry_raw / 0.5) * 0.5
    carry = min(carry, carry_floor)

    return float(carry)


def compute_paid_status(
    *,
    employee,
    leave_type: str,
    leave_days: float,
    start_date: date,
    end_date: date,
    instance_id: Optional[int] = None,
) -> bool:
    leave_type = normalize_leave_type(leave_type)

    if leave_type == "WFH":
        return False

    limits = get_leave_limits()
    if leave_type not in limits:
        return False

    # probation => always unpaid
    if employee.probation_end_date and start_date <= employee.probation_end_date:
        return False

    leave_year_start, leave_year_end_excl = get_leave_year_range_for_employee(employee, on_date=start_date)

    vacation_carry = 0.0
    if leave_type == "VACATION":
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(employee, on_date=prev_day)
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=leave_type,
        status="APPROVED",
        is_paid=True,
        start_date__lt=leave_year_end_excl,
        end_date__gte=leave_year_start,
    )

    if instance_id:
        qs = qs.exclude(id=instance_id)

    used = sum(overlapping_days(lr, leave_year_start, leave_year_end_excl) for lr in qs)

    total_allowed = float(limits[leave_type]) + float(vacation_carry)
    remaining = max(total_allowed - float(used), 0.0)

    return float(leave_days) <= float(remaining)

