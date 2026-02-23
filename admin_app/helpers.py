from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Dict, Optional

from django.db.models import Q
from django.utils import timezone

from form_app.helpers import overlapping_days, display_is_paid, _norm_status_expr
from form_app.models import LeaveRequest
from form_app.policies import get_leave_limits_for_employee
from user_app.helpers import get_leave_year_range, carry_forward_only


SESSION_MAP = {
    "FULL": "FULL",
    "AM": "MORNING",
    "PM": "EVENING",
}


# Small utilities
def _strip(x) -> str:
    return (x or "").strip()


def _parse_int(x: str) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _parse_bool(x) -> Optional[bool]:

    if x is None:
        return None
    v = _strip(str(x)).lower()
    if v in ("1", "true", "yes", "y"):
        return True
    if v in ("0", "false", "no", "n"):
        return False
    return None


def _looks_like_sort_dir(x) -> bool:

    if x is None:
        return False
    v = _strip(str(x)).lower()
    return v in {"asc", "desc", "ascending", "descending"}


def _normalize_sort_dir(x) -> Optional[str]:

    if not _looks_like_sort_dir(x):
        return None
    v = _strip(str(x)).lower()
    return "desc" if v in {"desc", "descending"} else "asc"


def _month_window(year: int, month: int) -> tuple[date, date]:

    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


# Filters


def apply_request_filters(qs, params):
    name = (
        _strip(params.get("name"))
        or _strip(params.get("search"))
        or _strip(params.get("q"))
    )

    month_raw = _strip(params.get("month"))
    year_raw = _strip(params.get("year"))
    leave_type = _strip(params.get("leave_type"))
    status_val = _strip(params.get("status"))
    paid_raw = params.get("paid") or params.get("is_paid")

    # Guard: some UIs incorrectly reuse filter keys for sorting
    # (e.g. leave_type=asc, year=desc). Treat these as non-filters.
    if _looks_like_sort_dir(month_raw):
        month_raw = ""
    if _looks_like_sort_dir(year_raw):
        year_raw = ""
    if _looks_like_sort_dir(leave_type):
        leave_type = ""
    if _looks_like_sort_dir(status_val):
        status_val = ""

    # year-only filter
    if year_raw and not month_raw:
        year = _parse_int(year_raw)
        if year is None or year < 1900 or year > 3000:
            return qs.none()

        y_start = date(year, 1, 1)
        y_end = date(year, 12, 31)
        qs = qs.filter(start_date__lte=y_end, end_date__gte=y_start)

    # month/year filter
    if month_raw:
        month = None
        year = None

        if "-" in month_raw and not year_raw:
            parts = month_raw.split("-", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                year = _parse_int(parts[0])
                month = _parse_int(parts[1])

        if month is None:
            month = _parse_int(month_raw)
        if month is None or not (1 <= month <= 12):
            return qs.none()

        if year is None:
            year = _parse_int(year_raw) if year_raw else timezone.localdate().year
        if year is None or year < 1900 or year > 3000:
            return qs.none()

        m_start, m_end = _month_window(year, month)
        # overlap: start <= m_end AND end >= m_start
        qs = qs.filter(start_date__lte=m_end, end_date__gte=m_start)

    # status filter (normalized)
    if status_val:
        wanted = status_val.upper()
        # If status is not one of the known choices, ignore it.
        if wanted in {
            LeaveRequest.STATUS_PENDING,
            LeaveRequest.STATUS_APPROVED,
            LeaveRequest.STATUS_REJECTED,
            LeaveRequest.STATUS_VOIDED,
        }:
            qs = qs.annotate(_status_norm=_norm_status_expr("status")).filter(
                _status_norm=wanted
            )

    # leave type filter
    if leave_type:
        wanted_type = leave_type.strip().upper()
        allowed_types = {
            str(v).strip().upper() for (v, _label) in LeaveRequest.LEAVE_TYPE_CHOICES
        }
        if wanted_type in allowed_types:
            qs = qs.filter(leave_type__iexact=wanted_type)

    # paid filter
    paid_bool = _parse_bool(paid_raw)
    if paid_bool is not None:
        qs = qs.filter(is_paid=paid_bool)

    # name search
    if name:
        qs = qs.filter(
            Q(employee__name__icontains=name)
            | Q(employee__user__first_name__icontains=name)
            | Q(employee__user__last_name__icontains=name)
            | Q(employee__user__username__icontains=name)
        )

    return qs


# Shared computations
def get_employee_leaves(emp) -> list[LeaveRequest]:

    if not emp:
        return []

    leaves = getattr(emp, "prefetched_leaves", None)
    if leaves is None:
        leaves = list(
            LeaveRequest.objects.filter(employee=emp).order_by("-applied_at", "-id")
        )
    return list(leaves)


def get_leave_year_window(emp, today: Optional[date] = None) -> tuple[date, date]:

    today = today or timezone.localdate()
    if not emp:
        return today, today
    return get_leave_year_range(emp, today)


def vacation_carry_forward(emp, year_start: date) -> float:
    if not emp:
        return 0.0

    # if employee is on probation => no carry forward
    is_on_prob = getattr(emp, "is_on_probation", None)
    if callable(is_on_prob) and is_on_prob():
        return 0.0

    prev_day = year_start - timedelta(days=1)
    prev_start, prev_end_excl = get_leave_year_range(emp, prev_day)
    return float(carry_forward_only(emp, prev_start, prev_end_excl))


def requested_days(lr: LeaveRequest) -> float:

    total_days_fn = getattr(lr, "total_days", None)
    if callable(total_days_fn):
        try:
            return float(total_days_fn())
        except Exception:
            pass

    if not lr.start_date or not lr.end_date:
        return 0.0

    if lr.start_date == lr.end_date and (lr.session or "").strip().upper() in (
        "AM",
        "PM",
    ):
        return 0.5

    return float((lr.end_date - lr.start_date).days + 1)


def total_leave_this_year(emp, leaves: Optional[list[LeaveRequest]] = None) -> float:
    if not emp:
        return 0.0

    leaves = leaves if leaves is not None else get_employee_leaves(emp)
    year_start, year_end_excl = get_leave_year_window(emp)

    total = 0.0
    for lr in leaves:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue
        total += float(overlapping_days(lr, year_start, year_end_excl))

    return float(total)


def used_leaves_by_type(
    emp, leaves: Optional[list[LeaveRequest]] = None
) -> dict[str, float]:

    if not emp:
        return {}

    leaves = leaves if leaves is not None else get_employee_leaves(emp)
    year_start, year_end_excl = get_leave_year_window(emp)

    out: dict[str, float] = {}
    unpaid_total = 0.0

    for lr in leaves:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue

        days = float(overlapping_days(lr, year_start, year_end_excl))
        if days <= 0:
            continue

        if lr.is_paid:
            key = (lr.leave_type or "").strip().upper()
            out[key] = float(out.get(key, 0.0) + days)
        else:
            if (lr.leave_type or "").strip().upper() == "WFH":
                continue
            unpaid_total += float(days)

    if unpaid_total > 0:
        out["UNPAID"] = float(out.get("UNPAID", 0.0) + unpaid_total)

    return out


def remaining_leaves(
    emp, leaves: Optional[list[LeaveRequest]] = None
) -> dict[str, float]:

    if not emp:
        return {}

    leaves = leaves if leaves is not None else get_employee_leaves(emp)
    year_start, year_end_excl = get_leave_year_window(emp)

    used_paid_by_type: dict[str, float] = {}
    for lr in leaves:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue
        if not lr.is_paid:
            continue

        days = float(overlapping_days(lr, year_start, year_end_excl))
        if days <= 0:
            continue

        key = (lr.leave_type or "").strip().upper()
        used_paid_by_type[key] = float(used_paid_by_type.get(key, 0.0) + days)

    carry = vacation_carry_forward(emp, year_start)

    remaining: dict[str, float] = {}
    limits = get_leave_limits_for_employee(emp)
    for leave_type, yearly_limit in limits.items():
        leave_type_u = (leave_type or "").strip().upper()
        total_allowed = float(yearly_limit)

        if leave_type_u == "VACATION":
            total_allowed += float(carry)

        used = float(used_paid_by_type.get(leave_type_u, 0.0))
        remaining[leave_type_u] = round(max(total_allowed - used, 0.0), 1)

    return remaining


def remaining_balance_for_type(emp, leave_type: str) -> Optional[float]:

    if not emp:
        return None

    leave_type_u = (leave_type or "").strip().upper()
    limits = get_leave_limits_for_employee(emp)
    if leave_type_u not in limits:
        return None

    today = timezone.localdate()
    year_start, year_end_excl = get_leave_year_window(emp, today=today)

    allowed = float(limits[leave_type_u])
    if leave_type_u == "VACATION":
        allowed += vacation_carry_forward(emp, year_start)

    used = 0.0
    qs = LeaveRequest.objects.filter(
        employee=emp,
        is_paid=True,
        leave_type__iexact=leave_type_u,
    ).order_by("-applied_at", "-id")

    for lr in qs:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue
        used += float(overlapping_days(lr, year_start, year_end_excl))

    return round(max(allowed - used, 0.0), 1)


def serialize_request_for_frontend(lr: LeaveRequest) -> Dict[str, Any]:

    emp = lr.employee
    u = emp.user if emp else None

    session = SESSION_MAP.get(
        (lr.session or "").strip().upper(), (lr.session or "FULL")
    )
    req_days = requested_days(lr)

    current_balance = None
    after_approval = None

    if emp and lr.is_paid and lr.leave_type:
        current_balance = remaining_balance_for_type(emp, lr.leave_type)
        if current_balance is not None:
            after_approval = round(
                max(float(current_balance) - float(req_days), 0.0), 1
            )

    return {
        "user": {
            "username": (u.username if u else "Unknown") or "Unknown",
            "firstname": (u.first_name if u else "") or "",
            "lastname": (u.last_name if u else "") or "",
        },
        "formID": int(lr.id),
        "status": (lr.status or "").strip().upper(),
        "leave_type": (lr.leave_type or "").strip().upper(),
        "paid": display_is_paid(lr.leave_type, getattr(lr, "is_paid", None)),
        "session": session,
        "project": getattr(emp, "current_project", None) if emp else "",
        "start_date": lr.start_date.isoformat() if lr.start_date else None,
        "end_date": lr.end_date.isoformat() if lr.end_date else None,
        "number_of_days": float(req_days),
        "current_balance": current_balance,
        "after_approval": after_approval,
        "appliedAt": lr.applied_at.isoformat() if lr.applied_at else None,
        "reason": (getattr(lr, "reason", "") or "").strip(),
        "rejection_reason": (getattr(lr, "rejection_reason", "") or "").strip(),
    }
