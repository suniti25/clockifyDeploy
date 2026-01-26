from datetime import date, datetime, timedelta
from calendar import monthrange
from django.db.models import Q

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest

# LEAVE YEAR HELPERS


def year_reset(year: int, month: int, day: int) -> date:
    """
    Safe date builder for leap years (Feb 29 -> Feb 28 fallback)
    """
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def get_leave_year_range(probation_end_date: date, today: date):
    """
    Leave year is anchored to probation_end_date (month/day)
    Returns: (start_date, end_date_exclusive)
    """
    anchor_month = probation_end_date.month
    anchor_day = probation_end_date.day

    start_this_year = year_reset(today.year, anchor_month, anchor_day)

    if today >= start_this_year:
        start = start_this_year
        end = year_reset(today.year + 1, anchor_month, anchor_day)
    else:
        start = year_reset(today.year - 1, anchor_month, anchor_day)
        end = year_reset(today.year, anchor_month, anchor_day)

    return start, end


def carry_forward_only(employee, prev_start: date, prev_end: date) -> float:
    """
    Carry forward is ONLY for VACATION leave.
    Rule: 50% of unused VACATION leave
    """
    yearly_vacation = LEAVE_LIMITS.get("VACATION", 0)

    prev_used = sum(
        lr.total_days()
        for lr in LeaveRequest.objects.filter(
            employee=employee,
            leave_type="VACATION",
            status="APPROVED",
            is_paid=True,
            start_date__lt=prev_end,
            end_date__gte=prev_start,
        )
    )

    prev_remaining = max(yearly_vacation - prev_used, 0)
    carry = prev_remaining * 0.5

    # Supports half-day values (0.5)
    return round(carry, 1)


def approved_requests_in_window(employee, start: date, end_exclusive: date):
    """
    Returns approved + paid leave overlapping the leave-year window
    """
    end_inclusive = end_exclusive - timedelta(days=1)

    return LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        is_paid=True,
        start_date__lte=end_inclusive,
        end_date__gte=start,
    )


def group_used_by_type(approved_requests):
    """
    Groups total used days by leave_type
    """
    used = {}
    for req in approved_requests:
        used[req.leave_type] = used.get(req.leave_type, 0) + req.total_days()
    return used


# HISTORY HELPERS


def history_queryset(employee):
    """
    Base history query (newest → oldest)
    """
    return LeaveRequest.objects.filter(employee=employee).order_by("-applied_at")


def serialize_history_item(req: LeaveRequest):
    leave_name = req.get_leave_type_display()

    if req.status == "APPROVED":
        title = "Leave Approved"
        message = f"Your {leave_name.lower()} leave was approved."
    elif req.status == "REJECTED":
        title = "Leave Rejected"
        message = f"Your {leave_name.lower()} leave was rejected."
    else:
        title = "Leave Request Submitted"
        message = f"Your {leave_name.lower()} leave request is pending approval."

    return {
        "id": str(req.id),
        "leave_type": leave_name,
        "status": req.status,
        "start_date": req.start_date.isoformat(),
        "end_date": req.end_date.isoformat(),
        "days": float(req.total_days()),
        "session": req.get_session_display(),
        "is_paid": bool(req.is_paid),
        "applied_at": req.applied_at.isoformat() if req.applied_at else None,
        "title": title,
        "message": message,
        "reason": req.reason or "",
        "rejection_reason": getattr(req, "rejection_reason", "") or "",
    }


def parse_month_param(month_str: str):
    """
    Supports:
    - 2026-01
    - January 2026
    """
    if not month_str:
        return None, None

    month_str = month_str.strip()

    try:
        dt = datetime.strptime(month_str, "%Y-%m")
        start = date(dt.year, dt.month, 1)
        end = date(dt.year, dt.month, monthrange(dt.year, dt.month)[1]) + timedelta(days=1)
        return start, end
    except ValueError:
        pass

    try:
        dt = datetime.strptime(month_str, "%B %Y")
        start = date(dt.year, dt.month, 1)
        end = date(dt.year, dt.month, monthrange(dt.year, dt.month)[1]) + timedelta(days=1)
        return start, end
    except ValueError:
        return None, None


def apply_history_filters(qs, search="", month="", leave_type="", status_filter=""):
    if search:
        qs = qs.filter(leave_type__icontains=search)

    if status_filter:
        qs = qs.filter(status=status_filter.upper())

    if leave_type:
        qs = qs.filter(leave_type=leave_type.upper())

    if month:
        month = str(month).strip()

        # Best: month includes year, like "2026-01" or "January 2026"
        start, end = parse_month_param(month)
        if start and end:
            qs = qs.filter(start_date__gte=start, start_date__lt=end)
        else:
            # Fallback: month is numeric only ("1".."12") -> mixes years by nature
            try:
                qs = qs.filter(start_date__month=int(month))
            except ValueError:
                # Ignore invalid month filter input and return unfiltered queryset
                return qs

    return qs
