from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from form_app.policies import (
    get_leave_limits_for_employee,
    get_carryover_percentage,
    get_leave_year_range_for_employee,
)
from form_app.helpers import (
    overlapping_days as form_overlapping_days,
    display_is_paid,
    compute_paid_unpaid_split_for_request,
    _norm_status_expr,
)
from form_app.models import LeaveRequest

from .models import Profile

# LEAVE YEAR HELPERS


def get_leave_year_range(employee, today: date) -> tuple[date, date]:
    return get_leave_year_range_for_employee(employee, on_date=today)


def _round_to_half_day(value: float) -> float:

    d = Decimal(str(value))
    half = Decimal("0.5")
    return float((d / half).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * half)


def carry_forward_only(employee, prev_start: date, prev_end_exclusive: date) -> float:

    if bool(getattr(employee, "reset_leave_balance", False)):
        return 0.0

    joining_date = getattr(employee, "joining_date", None)
    # Eligibility: carry-forward applies only if the employee was employed for the
    # full previous leave year window.
    if joining_date and joining_date > prev_start:
        return 0.0

    limits = get_leave_limits_for_employee(employee)
    yearly_vacation = float(limits.get("VACATION", 0.0))

    prev_used = 0.0
    qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type="VACATION",
        status="APPROVED",
        is_paid=True,
        start_date__lt=prev_end_exclusive,
        end_date__gte=prev_start,
    )

    for lr in qs:
        prev_used += float(lr.total_days())

    prev_remaining = max(yearly_vacation - prev_used, 0.0)
    carry_pct = max(0, min(get_carryover_percentage(), 100))
    carry_raw = prev_remaining * (float(carry_pct) / 100.0)

    # round to nearest 0.5
    carry = _round_to_half_day(carry_raw)
    carry_floor = int(carry_raw / 0.5) * 0.5
    carry = min(carry, carry_floor)

    return float(carry)


# HISTORY HELPER
def history_queryset(employee):

    return (
        LeaveRequest.objects.filter(employee=employee)
        .select_related("employee")
        .order_by("-applied_at", "-id")
    )


def _looks_like_sort_dir(x) -> bool:
    if x is None:
        return False
    v = str(x).strip().lower()
    return v in {"asc", "desc", "ascending", "descending"}


def parse_month_param(month_str: str) -> tuple[Optional[date], Optional[date]]:

    if not month_str:
        return None, None

    s = str(month_str).strip()
    if not s:
        return None, None

    # Formats with year
    for fmt in ("%Y-%m", "%B %Y", "%b %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            start = date(dt.year, dt.month, 1)
            end_excl = (
                date(dt.year + 1, 1, 1)
                if dt.month == 12
                else date(dt.year, dt.month + 1, 1)
            )
            return start, end_excl
        except ValueError:
            continue

    # Month only:assume current year
    try:
        m = int(s)
        if 1 <= m <= 12:
            today = datetime.now().date()
            start = date(today.year, m, 1)
            end_excl = (
                date(today.year + 1, 1, 1) if m == 12 else date(today.year, m + 1, 1)
            )
            return start, end_excl
    except ValueError:
        return None, None

    return None, None


def apply_history_filters(
    qs, search: str = "", month: str = "", leave_type: str = "", status_filter: str = ""
):

    search = (search or "").strip()
    month = (month or "").strip()
    leave_type = (leave_type or "").strip()
    status_filter = (status_filter or "").strip()

    # Guard: some UIs reuse filter keys for sorting (e.g. type=asc)
    if _looks_like_sort_dir(month):
        month = ""
    if _looks_like_sort_dir(leave_type):
        leave_type = ""
    if _looks_like_sort_dir(status_filter):
        status_filter = ""

    if search:
        qs = qs.filter(
            Q(leave_type__icontains=search)
            | Q(reason__icontains=search)
            | Q(status__icontains=search)
        )

    if leave_type:
        qs = qs.filter(leave_type__iexact=leave_type)

    if status_filter:
        wanted = status_filter.upper()
        qs = qs.annotate(_status_norm=_norm_status_expr("status")).filter(
            _status_norm=wanted
        )

    if month:
        start, end_excl = parse_month_param(month)
        if start and end_excl:
            #  overlap-based month filter
            qs = qs.filter(start_date__lt=end_excl, end_date__gte=start)

    return qs


# VIEW HELPERS
@dataclass(frozen=True)
class LeaveYearContext:
    today: date
    leave_year_start: date
    leave_year_end_excl: date
    leave_year_end_incl: date
    vacation_carry: float
    is_on_probation: bool


SESSION_LABEL = {
    "FULL": "Full Day",
    "FD": "Full Day",
    "AM": "Morning",
    "PM": "Afternoon",
}


def _session_label(session: str | None) -> str:
    key = (session or "FULL").strip().upper()
    return SESSION_LABEL.get(key, key)


def _title_for_status(status_val: str | None) -> str:
    s = (status_val or "").strip().upper()
    if s == "APPROVED":
        return "Leave Approved"
    if s == "REJECTED":
        return "Leave Rejected"
    if s == "PENDING":
        return "Leave Pending"
    if s == "VOIDED":
        return "Leave Voided"
    return "Leave Update"


def _message_for_status(leave_type_label: str, status_val: str | None) -> str:
    s = (status_val or "").strip().upper()
    lt = (leave_type_label or "leave").lower()
    if s == "APPROVED":
        return f"Your {lt} leave was approved."
    if s == "REJECTED":
        return f"Your {lt} leave was rejected."
    if s == "PENDING":
        return f"Your {lt} leave is pending."
    if s == "VOIDED":
        return f"Your {lt} leave was voided."
    return f"Update for your {lt} leave."


def _get_profile_and_employee(request):

    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return (
            None,
            None,
            Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND),
        )

    employee = profile.employee
    if not employee:
        return (
            profile,
            None,
            Response(
                {"error": "Employee record not found"}, status=status.HTTP_404_NOT_FOUND
            ),
        )

    return profile, employee, None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _build_leave_year_context(employee) -> LeaveYearContext:
    today = timezone.localdate()

    leave_year_start, leave_year_end_excl = get_leave_year_range(employee, today)
    leave_year_end_incl = leave_year_end_excl - timedelta(days=1)

    is_on_probation = employee.is_on_probation(on_date=today)

    vacation_carry = 0.0
    if not is_on_probation:
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range(employee, prev_day)
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    return LeaveYearContext(
        today=today,
        leave_year_start=leave_year_start,
        leave_year_end_excl=leave_year_end_excl,
        leave_year_end_incl=leave_year_end_incl,
        vacation_carry=vacation_carry,
        is_on_probation=is_on_probation,
    )


def _approved_requests_in_year(employee, ctx: LeaveYearContext):
    return LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        start_date__lt=ctx.leave_year_end_excl,
        end_date__gte=ctx.leave_year_start,
    )


def _aggregate_approved_usage(employee, ctx: LeaveYearContext):
    approved_qs = _approved_requests_in_year(employee, ctx).order_by(
        "start_date", "end_date", "id"
    )

    paid_used_by_type: dict[str, float] = {}
    probation_leave_total = 0.0
    unpaid_leave_total = 0.0

    limits = get_leave_limits_for_employee(employee)
    total_allowed_by_type: dict[str, float] = {
        lt: float(limit) for lt, limit in limits.items()
    }
    if "VACATION" in total_allowed_by_type:
        total_allowed_by_type["VACATION"] = float(limits.get("VACATION", 0.0)) + float(
            ctx.vacation_carry
        )

    for lr in approved_qs:
        days_in_window = float(
            form_overlapping_days(lr, ctx.leave_year_start, ctx.leave_year_end_excl)
        )
        if days_in_window <= 0:
            continue

        lt = (lr.leave_type or "").strip().upper()

        if lt == "WFH":
            continue

        in_probation = (
            bool(getattr(employee, "joining_date", None))
            and bool(getattr(employee, "probation_end_date", None))
            and employee.joining_date <= lr.start_date <= employee.probation_end_date
        )

        if in_probation:
            probation_leave_total += days_in_window
            continue

        if lt not in total_allowed_by_type:
            unpaid_leave_total += days_in_window
            continue

        remaining = max(
            total_allowed_by_type[lt] - float(paid_used_by_type.get(lt, 0.0)), 0.0
        )
        paid_portion = min(remaining, days_in_window)
        unpaid_portion = max(days_in_window - paid_portion, 0.0)

        if paid_portion > 0:
            paid_used_by_type[lt] = float(paid_used_by_type.get(lt, 0.0) + paid_portion)
        if unpaid_portion > 0:
            unpaid_leave_total += unpaid_portion

    return paid_used_by_type, probation_leave_total, unpaid_leave_total


def _leave_balance_list(
    employee,
    ctx: LeaveYearContext,
    paid_used_by_type: dict[str, float],
    probation_leave_total: float,
    unpaid_leave_total: float,
):

    balances: list[dict] = [
        {
            "type": "Probation Leave",
            "leave_year_start": ctx.leave_year_start.isoformat(),
            "leave_year_end": ctx.leave_year_end_incl.isoformat(),
            "carry_forward": 0.0,
            "total": 0.0,
            "used": round(probation_leave_total, 1),
            "remaining": 0.0,
        },
        {
            "type": "Unpaid Leave",
            "leave_year_start": ctx.leave_year_start.isoformat(),
            "leave_year_end": ctx.leave_year_end_incl.isoformat(),
            "carry_forward": 0.0,
            "total": 0.0,
            "used": round(unpaid_leave_total, 1),
            "remaining": 0.0,
        },
    ]

    limits = get_leave_limits_for_employee(employee)
    for leave_type, yearly_limit in limits.items():
        paid_used = float(paid_used_by_type.get(leave_type, 0.0))

        carry_forward = 0.0
        total_allowed = float(yearly_limit)

        if leave_type == "VACATION":
            carry_forward = float(ctx.vacation_carry)
            total_allowed += carry_forward

        remaining = max(total_allowed - paid_used, 0.0)

        balances.append(
            {
                "type": leave_type.capitalize(),
                "leave_year_start": ctx.leave_year_start.isoformat(),
                "leave_year_end": ctx.leave_year_end_incl.isoformat(),
                "carry_forward": round(carry_forward, 1),
                "total": round(total_allowed, 1),
                "used": round(paid_used, 1),
                "remaining": round(remaining, 1),
            }
        )

    return balances


def _serialize_upcoming(req: LeaveRequest) -> dict:
    label = (
        req.get_leave_type_display()
        if hasattr(req, "get_leave_type_display")
        else (req.leave_type or "")
    )
    paid_days, unpaid_days = compute_paid_unpaid_split_for_request(
        employee=req.employee, req=req
    )
    return {
        "id": str(req.id),
        "leave_type": label,
        "start_date": req.start_date.isoformat() if req.start_date else None,
        "end_date": req.end_date.isoformat() if req.end_date else None,
        "days": float(req.total_days()) if hasattr(req, "total_days") else None,
        "paid_days": float(paid_days),
        "unpaid_days": float(unpaid_days),
        "status": (req.status or "").strip().upper(),
        "message": f"{label} leave upcoming",
    }


def _serialize_recent(req: LeaveRequest) -> dict:
    leave_type_label = (
        req.get_leave_type_display()
        if hasattr(req, "get_leave_type_display")
        else (req.leave_type or "")
    )
    applied_at = getattr(req, "applied_at", None)
    reviewed_at = getattr(req, "reviewed_at", None) or getattr(req, "approved_at", None)
    paid_days, unpaid_days = compute_paid_unpaid_split_for_request(
        employee=req.employee, req=req
    )

    return {
        "id": str(req.id),
        "leave_type": leave_type_label,
        "status": (req.status or "").strip().upper(),
        "start_date": req.start_date.isoformat() if req.start_date else None,
        "end_date": req.end_date.isoformat() if req.end_date else None,
        "days": float(req.total_days()) if hasattr(req, "total_days") else None,
        "paid_days": float(paid_days),
        "unpaid_days": float(unpaid_days),
        "session": _session_label(getattr(req, "session", None)),
        "is_paid": display_is_paid(req.leave_type, getattr(req, "is_paid", None)),
        "applied_at": applied_at.isoformat() if applied_at else None,
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        "title": _title_for_status(getattr(req, "status", None)),
        "message": _message_for_status(leave_type_label, getattr(req, "status", None)),
        "reason": (getattr(req, "reason", "") or "").strip(),
        "rejection_reason": (getattr(req, "rejection_reason", "") or "").strip(),
    }
