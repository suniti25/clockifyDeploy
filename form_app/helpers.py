from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from django.db.models import CharField, F, Value
from django.db.models.functions import Replace, Trim, Upper
from django.utils.timezone import localdate
from rest_framework import serializers

from form_app.policies import (
    get_leave_limits_for_employee,
    get_carryover_percentage,
    get_leave_year_range_for_employee,
)
from form_app.models import LeaveRequest

MAX_FUTURE_DAYS = 365
MAX_LEAVE_DAYS = 60


def _is_probation_leave(*, employee, leave_start: date) -> bool:
    probation_end = getattr(employee, "probation_end_date", None)
    if not probation_end:
        return False
    try:
        return bool(leave_start and leave_start <= probation_end)
    except Exception:
        return False


def compute_paid_unpaid_split(
    *,
    employee,
    leave_type: str,
    leave_days: float,
    start_date: date,
    end_date: date,
    instance_id: Optional[int] = None,
) -> tuple[float, float, float]:
    """
    It includes all approved leaves of the same type,
    whether they were paid or unpaid.
    This ensures that even if a leave exceeded the limit
    and was marked unpaid, it still correctly reduces
    the employee’s future paid leave balance.

    """

    leave_type = normalize_leave_type(leave_type)
    leave_days = float(leave_days or 0.0)

    if leave_type == "WFH" or leave_days <= 0.0:
        return 0.0, 0.0, 0.0

    limits = get_leave_limits_for_employee(employee)
    if leave_type not in limits:
        return 0.0, leave_days, 0.0

    if _is_probation_leave(employee=employee, leave_start=start_date):
        return 0.0, leave_days, 0.0

    leave_year_start, leave_year_end_excl = get_leave_year_range_for_employee(
        employee, on_date=start_date
    )

    vacation_carry = 0.0
    if leave_type == "VACATION":
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(
            employee, on_date=prev_day
        )
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    total_allowed = float(limits[leave_type]) + float(vacation_carry)

    approved_qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=leave_type,
        status="APPROVED",
        start_date__lt=leave_year_end_excl,
        end_date__gte=leave_year_start,
    )

    probation_end = getattr(employee, "probation_end_date", None)
    if probation_end:
        approved_qs = approved_qs.filter(start_date__gt=probation_end)

    if instance_id:
        approved_qs = approved_qs.exclude(id=instance_id)

    used_days = sum(
        overlapping_days(lr, leave_year_start, leave_year_end_excl)
        for lr in approved_qs
    )
    remaining = max(total_allowed - float(used_days), 0.0)

    paid_days = min(float(leave_days), float(remaining))
    unpaid_days = max(float(leave_days) - float(paid_days), 0.0)

    # Keep consistent half-day rounding for UI.
    paid_days = _round_to_half_day(paid_days)
    unpaid_days = _round_to_half_day(unpaid_days)
    remaining = _round_to_half_day(remaining)

    # Guard rounding drift
    if paid_days + unpaid_days > leave_days + 1e-9:
        unpaid_days = max(_round_to_half_day(leave_days - paid_days), 0.0)

    return float(paid_days), float(unpaid_days), float(remaining)


def compute_paid_unpaid_split_for_request(
    *, employee, req: LeaveRequest
) -> tuple[float, float]:
    """Compute paid/unpaid split for a persisted leave request."""
    leave_days = float(req.total_days()) if hasattr(req, "total_days") else 0.0
    lt = normalize_leave_type(getattr(req, "leave_type", None))
    if lt == "WFH" or leave_days <= 0.0:
        return 0.0, 0.0

    leave_year_start, leave_year_end_excl = get_leave_year_range_for_employee(
        employee, on_date=req.start_date
    )
    limits = get_leave_limits_for_employee(employee)
    if lt not in limits:
        return 0.0, leave_days

    if _is_probation_leave(employee=employee, leave_start=req.start_date):
        return 0.0, leave_days

    vacation_carry = 0.0
    if lt == "VACATION":
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(
            employee, on_date=prev_day
        )
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    total_allowed = float(limits[lt]) + float(vacation_carry)
    remaining = float(total_allowed)

    status_norm = (getattr(req, "status", "") or "").strip().upper()

    # We simulate consumption in chronological order so an already-approved leave
    # gets a stable split based on what came before it.
    approved_qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=lt,
        status="APPROVED",
        start_date__lt=leave_year_end_excl,
        end_date__gte=leave_year_start,
    ).order_by("start_date", "end_date", "id")

    probation_end = getattr(employee, "probation_end_date", None)
    if probation_end:
        approved_qs = approved_qs.filter(start_date__gt=probation_end)

    if status_norm != "APPROVED":
        # Pending/rejected/voided: request hasn't consumed yet.
        approved_qs = approved_qs.exclude(id=req.id)

    paid_for_req = None
    unpaid_for_req = None

    for lr in approved_qs:
        lr_days = float(overlapping_days(lr, leave_year_start, leave_year_end_excl))
        if lr_days <= 0.0:
            continue

        paid_portion = min(remaining, lr_days)
        unpaid_portion = max(lr_days - paid_portion, 0.0)
        remaining = max(remaining - paid_portion, 0.0)

        if lr.id == req.id:
            paid_for_req = paid_portion
            unpaid_for_req = unpaid_portion
            break

    if status_norm != "APPROVED":
        # Preview for a not-yet-approved request.
        paid_for_req = min(remaining, leave_days)
        unpaid_for_req = max(leave_days - paid_for_req, 0.0)

    paid_days = _round_to_half_day(float(paid_for_req or 0.0))
    unpaid_days = _round_to_half_day(float(unpaid_for_req or 0.0))

    # Ensure unpaid only appears if it actually exceeds.
    if unpaid_days < 0.0:
        unpaid_days = 0.0
    if unpaid_days > 0.0 and paid_days + unpaid_days < leave_days - 1e-9:
        unpaid_days = _round_to_half_day(max(leave_days - paid_days, 0.0))

    return float(paid_days), float(unpaid_days)


def _norm_status_expr(field_name: str = "status"):
    return Upper(
        Trim(
            Replace(
                F(field_name),
                Value("\u00a0"),
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
        raise serializers.ValidationError(
            "You cannot apply leave with a start date in the past."
        )

    if start_date > today + timedelta(days=MAX_FUTURE_DAYS):
        raise serializers.ValidationError(
            f"You cannot apply leave more than {MAX_FUTURE_DAYS} days in the future."
        )

    if end_date < start_date:
        raise serializers.ValidationError("End date cannot be earlier than start date.")

    total_days = (end_date - start_date).days + 1
    if total_days > MAX_LEAVE_DAYS:
        raise serializers.ValidationError(
            f"Leave duration cannot exceed {MAX_LEAVE_DAYS} days."
        )

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

    overlapping = LeaveRequest.objects.annotate(
        _status_norm=_norm_status_expr("status")
    ).filter(
        employee=employee,
        start_date__lte=end_date,
        end_date__gte=start_date,
        _status_norm__in=[LeaveRequest.STATUS_PENDING, LeaveRequest.STATUS_APPROVED],
    )

    if instance_id:
        overlapping = overlapping.exclude(id=instance_id)

    if allow_overlap_with_id:
        overlapping = overlapping.exclude(id=allow_overlap_with_id)

    if overlapping.exists():
        raise serializers.ValidationError(
            "You already have a leave applied for this date range."
        )

    return normalized_session, normalized_leave_type


def compute_leave_days_for_payload(*, employee, payload: dict) -> float:
    temp = LeaveRequest(employee=employee, **payload)
    return float(temp.total_days())


def _round_to_half_day(value: float) -> float:
    d = Decimal(str(value))
    half = Decimal("0.5")
    return float((d / half).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * half)


def overlapping_days(
    req: LeaveRequest, window_start: date, window_end_exclusive: date
) -> float:
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
    if (
        overlap_start == req.start_date
        and start_sess == "PM"
        and overlap_start.weekday() < 5
    ):
        days -= 0.5
    if overlap_end == req.end_date and end_sess == "AM" and overlap_end.weekday() < 5:
        days -= 0.5

    return max(days, 0.0)


def carry_forward_only(employee, prev_start: date, prev_end_exclusive: date) -> float:
    if bool(getattr(employee, "reset_leave_balance", False)):
        return 0.0

    limits = get_leave_limits_for_employee(employee)
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

    limits = get_leave_limits_for_employee(employee)
    if leave_type not in limits:
        return False

    # probation => always unpaid
    if employee.probation_end_date and start_date <= employee.probation_end_date:
        return False

    _paid_days, unpaid_days, _remaining = compute_paid_unpaid_split(
        employee=employee,
        leave_type=leave_type,
        leave_days=float(leave_days),
        start_date=start_date,
        end_date=end_date,
        instance_id=instance_id,
    )

    # "Paid" means: the entire request is within remaining balance.
    return float(leave_days or 0.0) > 0.0 and unpaid_days <= 0.0
