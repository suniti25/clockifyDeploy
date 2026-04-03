from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Dict, Optional

from django.db.models import Q
from django.utils import timezone

from form_app.helpers import (
    overlapping_days,
    display_is_paid,
    _norm_status_expr,
    compute_paid_unpaid_split_for_request,
    get_leave_selected_dates,
    get_manual_topup_by_type,
    get_manual_used_by_type,
    fmt_leave_days,
)

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
    # Treat these as non-filters.
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

    # status filter
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
            LeaveRequest.objects.filter(employee=emp)
            .prefetch_related("days")
            .order_by("-applied_at", "-id")
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
    paid_used_by_type, probation_total, unpaid_total, _carry, _ys, _ye = (
        _aggregate_approved_usage(emp, leaves=leaves)
    )

    total = (
        float(sum(paid_used_by_type.values()))
        + float(probation_total)
        + float(unpaid_total)
    )
    return fmt_leave_days(float(round(total, 1)))


def _aggregate_approved_usage(
    emp,
    *,
    leaves: Optional[list[LeaveRequest]] = None,
    today: Optional[date] = None,
) -> tuple[dict[str, float], float, float, float, date, date]:
    """Mirror employee dashboard usage logic for admin-side stats.
    Rules:
    - Only APPROVED requests count.
    - Skip WFH.
    - If request is during probation window, count as probation leave.
    - Paid usage is capped by allowed limits (VACATION includes carry-forward).
    - Any overflow beyond allowed limits becomes unpaid.
    """

    if not emp:
        today = today or timezone.localdate()
        return {}, 0.0, 0.0, 0.0, today, today

    leaves = leaves if leaves is not None else get_employee_leaves(emp)
    year_start, year_end_excl = get_leave_year_window(emp, today=today)

    carry = float(vacation_carry_forward(emp, year_start))

    limits = get_leave_limits_for_employee(emp)
    total_allowed_by_type: dict[str, float] = {
        (lt or "").strip().upper(): float(limit) for lt, limit in limits.items()
    }
    if "VACATION" in total_allowed_by_type:
        total_allowed_by_type["VACATION"] = float(
            total_allowed_by_type["VACATION"]
        ) + float(carry)

    paid_used_by_type: dict[str, float] = {}
    probation_total = 0.0
    unpaid_total = 0.0

    # Apply admin-entered manual usage as baseline used days.
    manual_used = get_manual_used_by_type(employee=emp, leave_year_start=year_start)
    manual_topup = get_manual_topup_by_type(employee=emp, leave_year_start=year_start)
    for lt, topup in manual_topup.items():
        if lt in total_allowed_by_type:
            total_allowed_by_type[lt] = float(total_allowed_by_type[lt]) + float(topup)
    for lt, allowed_total in total_allowed_by_type.items():
        mu = float(manual_used.get(lt, 0.0) or 0.0)
        if mu <= 0.0:
            continue
        if mu > float(allowed_total):
            paid_used_by_type[lt] = float(allowed_total)
            unpaid_total += float(mu - float(allowed_total))
        else:
            paid_used_by_type[lt] = float(mu)

    joining_date = getattr(emp, "joining_date", None)
    probation_end_date = getattr(emp, "probation_end_date", None)

    for lr in leaves:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue

        days = float(overlapping_days(lr, year_start, year_end_excl))
        if days <= 0:
            continue

        lt = (lr.leave_type or "").strip().upper()
        if lt == "WFH":
            continue

        in_probation = (
            bool(joining_date)
            and bool(probation_end_date)
            and bool(getattr(lr, "start_date", None))
            and joining_date <= lr.start_date <= probation_end_date
        )
        if in_probation:
            probation_total += days
            continue

        if lt not in total_allowed_by_type:
            unpaid_total += days
            continue

        remaining = max(
            float(total_allowed_by_type[lt]) - float(paid_used_by_type.get(lt, 0.0)),
            0.0,
        )
        paid_portion = min(remaining, days)
        unpaid_portion = max(days - paid_portion, 0.0)

        if paid_portion > 0:
            paid_used_by_type[lt] = float(paid_used_by_type.get(lt, 0.0) + paid_portion)
        if unpaid_portion > 0:
            unpaid_total += unpaid_portion

    return (
        paid_used_by_type,
        float(probation_total),
        float(unpaid_total),
        float(carry),
        year_start,
        year_end_excl,
    )


def used_leaves_by_type(
    emp, leaves: Optional[list[LeaveRequest]] = None
) -> dict[str, float]:

    if not emp:
        return {}

    leaves = leaves if leaves is not None else get_employee_leaves(emp)

    paid_used_by_type, _probation_total, unpaid_total, _carry, _ys, _ye = (
        _aggregate_approved_usage(emp, leaves=leaves)
    )

    out: dict[str, float | int] = {
        k: fmt_leave_days(float(round(v, 1))) for k, v in paid_used_by_type.items()
    }
    if unpaid_total > 0:
        cur = float(out.get("UNPAID", 0.0) or 0.0)
        out["UNPAID"] = fmt_leave_days(float(round(cur + float(unpaid_total), 1)))
    return out


def total_leaves(emp) -> dict[str, float]:
    """Return annual leave allocation by type, including VACATION carry-forward."""
    if not emp:
        return {}

    year_start, _year_end_excl = get_leave_year_window(emp)
    carry = float(vacation_carry_forward(emp, year_start))
    manual_topup = get_manual_topup_by_type(employee=emp, leave_year_start=year_start)

    limits = get_leave_limits_for_employee(emp)
    out: dict[str, float] = {}
    for leave_type, limit in limits.items():
        leave_type_u = (leave_type or "").strip().upper()
        total_allowed = float(limit)
        if leave_type_u == "VACATION":
            total_allowed += float(carry)
        total_allowed += float(manual_topup.get(leave_type_u, 0.0) or 0.0)
        out[leave_type_u] = fmt_leave_days(float(round(total_allowed, 1)))
    return out


def remaining_leaves(
    emp, leaves: Optional[list[LeaveRequest]] = None
) -> dict[str, float]:

    if not emp:
        return {}

    leaves = leaves if leaves is not None else get_employee_leaves(emp)
    paid_used_by_type, _probation_total, _unpaid_total, carry, _ys, _ye = (
        _aggregate_approved_usage(emp, leaves=leaves)
    )

    remaining: dict[str, float] = {}
    limits = get_leave_limits_for_employee(emp)
    for leave_type, yearly_limit in limits.items():
        leave_type_u = (leave_type or "").strip().upper()
        total_allowed = float(yearly_limit)

        if leave_type_u == "VACATION":
            total_allowed += float(carry)

        used = float(paid_used_by_type.get(leave_type_u, 0.0))
        remaining[leave_type_u] = fmt_leave_days(
            float(round(max(total_allowed - used, 0.0), 1))
        )

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
    allowed += float(
        get_manual_topup_by_type(employee=emp, leave_year_start=year_start).get(
            leave_type_u, 0.0
        )
        or 0.0
    )

    manual_used = get_manual_used_by_type(
        employee=emp, leave_year_start=year_start
    ).get(leave_type_u, 0.0)
    used = float(manual_used)
    qs = LeaveRequest.objects.filter(
        employee=emp,
        is_paid=True,
        leave_type__iexact=leave_type_u,
    ).order_by("-applied_at", "-id")

    for lr in qs:
        if (lr.status or "").strip().upper() != LeaveRequest.STATUS_APPROVED:
            continue
        used += float(overlapping_days(lr, year_start, year_end_excl))

    return float(round(max(allowed - used, 0.0), 1))


def serialize_request_for_frontend(lr: LeaveRequest) -> Dict[str, Any]:

    emp = lr.employee
    u = emp.user if emp else None

    session = SESSION_MAP.get(
        (lr.session or "").strip().upper(), (lr.session or "FULL")
    )
    req_days = requested_days(lr)

    current_balance = None
    after_approval = None

    paid_days = 0.0
    unpaid_days = 0.0
    if emp:
        try:
            paid_days, unpaid_days = compute_paid_unpaid_split_for_request(
                employee=emp, req=lr
            )
        except Exception:
            paid_days, unpaid_days = 0.0, 0.0

    if emp and lr.is_paid and lr.leave_type:
        current_balance = remaining_balance_for_type(emp, lr.leave_type)
        if current_balance is not None:
            after_approval = round(
                max(float(current_balance) - float(req_days), 0.0), 1
            )

    selected_dates, is_selective = get_leave_selected_dates(lr)

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
        "dates": [d.isoformat() for d in selected_dates],
        "is_selective": bool(is_selective),
        "number_of_days": fmt_leave_days(float(req_days)),
        "paid_days": fmt_leave_days(float(paid_days)),
        "unpaid_days": fmt_leave_days(float(unpaid_days)),
        "current_balance": fmt_leave_days(current_balance),
        "after_approval": fmt_leave_days(after_approval),
        "appliedAt": lr.applied_at.isoformat() if lr.applied_at else None,
        "reason": (getattr(lr, "reason", "") or "").strip(),
        "rejection_reason": (getattr(lr, "rejection_reason", "") or "").strip(),
    }
