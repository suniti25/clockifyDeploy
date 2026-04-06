from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from django.db.models import Count
from django.utils import timezone

from form_app.models import LeaveRequest
from user_app.models import Employee

from .helpers import apply_request_filters, _norm_status_expr


# KPI SERVICES
WORKING_DAYS_PER_MONTH = 21

MONTH_ABBRS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _normalize_month_year(year: int | None, month: int | None) -> Tuple[int, int]:
    today = timezone.localdate()
    year_val = year if year is not None else today.year
    month_val = month if month is not None else today.month

    if month_val < 1 or month_val > 12:
        raise ValueError("Month must be between 1 and 12.")
    if year_val < 1900 or year_val > 3000:
        raise ValueError("Year must be between 1900 and 3000.")

    return year_val, month_val


def _month_window(year: int, month: int) -> Tuple[date, date]:
    month_start = date(year, month, 1)

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)

    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def parse_kpi_month_year_params(params) -> Tuple[int, int]:
    month_raw = params.get("month")
    year_raw = params.get("year")

    month = None
    year = None

    if month_raw not in (None, "") and year_raw in (None, ""):
        month_str = str(month_raw).strip()
        if "-" in month_str:
            parts = month_str.split("-", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                year = int(parts[0])
                month = int(parts[1])

    if month_raw not in (None, ""):
        if month is None:
            month = int(month_raw)
    if year_raw not in (None, ""):
        if year is None:
            year = int(year_raw)

    return _normalize_month_year(year, month)


def _overlap_days(
    start: date, end: date, window_start: date, window_end: date
) -> float:
    s = max(start, window_start)
    e = min(end, window_end)
    if s > e:
        return 0.0
    return float((e - s).days + 1)


def _leave_days_in_window(
    lr: LeaveRequest, window_start: date, window_end: date
) -> float:
    """
    Compute leave contribution inside a date window.
    Prefers per-day rows when present; otherwise falls back to boundary-session math.
    Weekends are excluded to match LeaveRequest.total_days behavior.
    """
    s = max(lr.start_date, window_start)
    e = min(lr.end_date, window_end)
    if s > e:
        return 0.0

    if lr.pk and hasattr(lr, "days"):
        try:
            day_rows = list(
                lr.days.filter(date__gte=s, date__lte=e).values_list("date", "session")
            )
        except Exception:
            day_rows = []

        if day_rows:
            total = 0.0
            for d, sess in day_rows:
                if not d or d.weekday() >= 5:
                    continue
                val = (sess or "FULL").strip().upper()
                if val == "FD":
                    val = "FULL"
                total += 0.5 if val in ("AM", "PM") else 1.0
            return max(total, 0.0)

    day_count = (e - s).days + 1
    weekdays = 0
    for i in range(day_count):
        if (s + timedelta(days=i)).weekday() < 5:
            weekdays += 1

    if weekdays <= 0:
        return 0.0

    session = (lr.session or "FULL").strip().upper()
    if session == "FD":
        session = "FULL"
    if session in ("AM", "PM"):
        return float(weekdays) * 0.5

    days = float(weekdays)
    start_sess = (lr.start_session or "FULL").strip().upper()
    end_sess = (lr.end_session or "FULL").strip().upper()
    if start_sess == "FD":
        start_sess = "FULL"
    if end_sess == "FD":
        end_sess = "FULL"

    # Apply boundary half-day adjustments only when the window includes
    # the corresponding request boundary date.
    if s == lr.start_date and start_sess == "PM" and s.weekday() < 5:
        days -= 0.5
    if e == lr.end_date and end_sess == "AM" and e.weekday() < 5:
        days -= 0.5

    return max(days, 0.0)


def _active_employee_count() -> int:
    return (
        Employee.objects.select_related("user")
        .filter(user__is_active=True)
        .exclude(user__is_staff=True)
        .exclude(user__is_superuser=True)
        .count()
    )


def sick_leave_kpi_for_current_month(
    *, year: int | None = None, month: int | None = None
) -> Dict[str, Any]:
    year, month = _normalize_month_year(year, month)
    month_start, month_end = _month_window(year, month)

    total_people = _active_employee_count()
    denom = WORKING_DAYS_PER_MONTH * total_people

    if denom == 0:
        return {
            "month": f"{year:04d}-{month:02d}",
            "working_days": WORKING_DAYS_PER_MONTH,
            "people": total_people,
            "total_sick_days": 0.0,
            "max_possible_days": 0,
            "sick_leave_percent": 0.0,
        }

    qs = LeaveRequest.objects.filter(
        leave_type="SICK",
        status="APPROVED",
        start_date__lte=month_end,
        end_date__gte=month_start,
    )

    total_sick_days = 0.0
    for lr in qs:
        total_sick_days += _overlap_days(
            lr.start_date, lr.end_date, month_start, month_end
        )

    percent = round((total_sick_days / denom) * 100, 2)

    return {
        "month": f"{year:04d}-{month:02d}",
        "working_days": WORKING_DAYS_PER_MONTH,
        "people": total_people,
        "total_sick_days": round(total_sick_days, 2),
        "max_possible_days": denom,
        "sick_leave_percent": percent,
    }


def vacation_leave_kpi_for_current_month(
    *, year: int | None = None, month: int | None = None
) -> Dict[str, Any]:
    year, month = _normalize_month_year(year, month)
    month_start, month_end = _month_window(year, month)

    total_people = _active_employee_count()
    denom = WORKING_DAYS_PER_MONTH * total_people

    if denom == 0:
        return {
            "month": f"{year:04d}-{month:02d}",
            "working_days": WORKING_DAYS_PER_MONTH,
            "people": total_people,
            "total_vacation_days": 0.0,
            "max_possible_days": 0,
            "vacation_leave_percent": 0.0,
        }

    qs = LeaveRequest.objects.filter(
        leave_type="VACATION",
        status="APPROVED",
        start_date__lte=month_end,
        end_date__gte=month_start,
    )

    total_vacation_days = 0.0
    for lr in qs:
        total_vacation_days += _overlap_days(
            lr.start_date, lr.end_date, month_start, month_end
        )

    percent = round((total_vacation_days / denom) * 100, 2)

    return {
        "month": f"{year:04d}-{month:02d}",
        "working_days": WORKING_DAYS_PER_MONTH,
        "people": total_people,
        "total_vacation_days": round(total_vacation_days, 2),
        "max_possible_days": denom,
        "vacation_leave_percent": percent,
    }


def wfh_leave_kpi_for_current_month(
    *, year: int | None = None, month: int | None = None
) -> Dict[str, Any]:
    year, month = _normalize_month_year(year, month)
    month_start, month_end = _month_window(year, month)

    total_people = _active_employee_count()
    denom = WORKING_DAYS_PER_MONTH * total_people

    if denom == 0:
        return {
            "month": f"{year:04d}-{month:02d}",
            "working_days": WORKING_DAYS_PER_MONTH,
            "people": total_people,
            "total_wfh_days": 0.0,
            "max_possible_days": 0,
            "wfh_percent": 0.0,
        }

    qs = LeaveRequest.objects.filter(
        leave_type="WFH",
        status="APPROVED",
        start_date__lte=month_end,
        end_date__gte=month_start,
    )

    total_wfh_days = 0.0
    for lr in qs:
        total_wfh_days += _overlap_days(
            lr.start_date, lr.end_date, month_start, month_end
        )

    percent = round((total_wfh_days / denom) * 100, 2)

    return {
        "month": f"{year:04d}-{month:02d}",
        "working_days": WORKING_DAYS_PER_MONTH,
        "people": total_people,
        "total_wfh_days": round(total_wfh_days, 2),
        "max_possible_days": denom,
        "wfh_percent": percent,
    }


def leave_trends_for_year(*, year: int | None = None) -> List[Dict[str, Any]]:

    year_val, _ = _normalize_month_year(year, 1)

    trends: List[Dict[str, Any]] = []
    for m in range(1, 13):
        sick = sick_leave_kpi_for_current_month(year=year_val, month=m)
        vacation = vacation_leave_kpi_for_current_month(year=year_val, month=m)
        wfh = wfh_leave_kpi_for_current_month(year=year_val, month=m)

        trends.append(
            {
                "month": f"{year_val:04d}-{m:02d}",
                "label": MONTH_ABBRS[m - 1],
                "vacation": vacation.get("vacation_leave_percent", 0.0),
                "sick": sick.get("sick_leave_percent", 0.0),
                "wfh": wfh.get("wfh_percent", 0.0),
            }
        )

    return trends


# ADMIN REQUESTS SERVICES
class AdminRequestServices:
    SORT_PARAM_KEYS = {
        "sort_by",
        "sort",
        "sort_dir",
        "sort_order",
        "order",
        "ordering",
    }

    @staticmethod
    def _strip_pagination_params(params):
        p = params.copy()
        p.pop("page", None)
        p.pop("page_size", None)
        # Sorting should never affect filter queryset.
        for k in AdminRequestServices.SORT_PARAM_KEYS:
            p.pop(k, None)
        return p

    @staticmethod
    def _parse_sort_dir(val) -> str | None:
        if val is None:
            return None
        v = str(val).strip().lower()
        if v in {"asc", "ascending"}:
            return "asc"
        if v in {"desc", "descending"}:
            return "desc"
        return None

    @staticmethod
    def ordering_for_params(params) -> list[str]:
        """Returns a Django order_by list for supported sorts.
        Also supports legacy UI patterns where the direction is sent
        as the value of the field key.
        """
        sort_by = params.get("sort_by") or params.get("sort")
        sort_dir = AdminRequestServices._parse_sort_dir(
            params.get("sort_dir") or params.get("sort_order") or params.get("order")
        )

        # Support ordering syntax like ordering=-start_date
        ordering_raw = params.get("ordering")
        if not sort_by and ordering_raw:
            o = str(ordering_raw).strip()
            if o.startswith("-"):
                sort_by = o[1:]
                sort_dir = sort_dir or "desc"
            else:
                sort_by = o
                sort_dir = sort_dir or "asc"

        if not sort_by:
            legacy_fields = [
                "leave_type",
                "paid",
                "is_paid",
                "paymentstatus",
                "payment_status",
                "start_date",
                "end_date",
                "applied_at",
                "appliedAt",
                "year",
                "month",
            ]
            for f in legacy_fields:
                d = AdminRequestServices._parse_sort_dir(params.get(f))
                if d:
                    sort_by = f
                    sort_dir = d
                    break

        if not sort_by:
            return ["-applied_at", "-id"]

        sort_dir = sort_dir or "asc"
        sort_by_norm = str(sort_by).strip().lower()

        field_map = {
            "leave_type": "leave_type",
            "paid": "is_paid",
            "is_paid": "is_paid",
            "paymentstatus": "is_paid",
            "payment_status": "is_paid",
            "start_date": "start_date",
            "end_date": "end_date",
            "applied_at": "applied_at",
            "appliedat": "applied_at",
            "dates": "start_date",
        }

        db_field = field_map.get(sort_by_norm)
        if not db_field:
            return ["-applied_at", "-id"]

        prefix = "-" if sort_dir == "desc" else ""
        ordering = [f"{prefix}{db_field}"]

        # Stable tie-breakers.
        if db_field != "applied_at":
            ordering.append("-applied_at")
        ordering.append("-id")
        return ordering

    @staticmethod
    def unfiltered_queryset():
        """
        Global queryset for cards. No filters must be applied here.
        """
        return LeaveRequest.objects.select_related(
            "employee", "employee__user"
        ).prefetch_related("days")

    @staticmethod
    def base_queryset(params):
        """
        Filtered queryset for the table/list. Pagination params are stripped.
        """
        filter_params = AdminRequestServices._strip_pagination_params(params)
        qs = LeaveRequest.objects.select_related(
            "employee", "employee__user"
        ).prefetch_related("days")
        return apply_request_filters(qs, filter_params)

    @staticmethod
    def get_status_counts(qs) -> Dict[str, int]:
        """
        TOTAL includes all statuses (including VOIDED).
        """
        norm_qs = qs.order_by().annotate(_s=_norm_status_expr("status"))
        raw = norm_qs.values("_s").annotate(c=Count("id"))

        counts: Dict[str, int] = {
            LeaveRequest.STATUS_PENDING: 0,
            LeaveRequest.STATUS_APPROVED: 0,
            LeaveRequest.STATUS_REJECTED: 0,
            LeaveRequest.STATUS_VOIDED: 0,
        }

        total = 0
        for row in raw:
            s = (row["_s"] or "").strip().upper()
            c = int(row["c"] or 0)
            if s in counts:
                counts[s] = c
            if s:
                total += c

        counts["TOTAL"] = total
        return counts

    @staticmethod
    def counts_for_params(params) -> Dict[str, int]:
        qs = AdminRequestServices.base_queryset(params)
        return AdminRequestServices.get_status_counts(qs)

    @staticmethod
    def top_leave_takers(
        params, days: int = 30, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Same filters except status.
        """
        filter_params = AdminRequestServices._strip_pagination_params(params).copy()
        filter_params["status"] = LeaveRequest.STATUS_APPROVED

        qs = LeaveRequest.objects.select_related(
            "employee", "employee__user"
        ).prefetch_related("days")
        qs = apply_request_filters(qs, filter_params)
        qs = qs.annotate(_status_norm=_norm_status_expr("status")).filter(
            _status_norm=LeaveRequest.STATUS_APPROVED
        )

        month_raw = params.get("month") if hasattr(params, "get") else None
        year_raw = params.get("year") if hasattr(params, "get") else None
        has_month_window = bool(str(month_raw or "").strip()) or bool(
            str(year_raw or "").strip()
        )

        month_start = None
        month_end = None

        if has_month_window:
            try:
                year, month = parse_kpi_month_year_params(params)
            except Exception:
                year, month = _normalize_month_year(None, None)
            month_start, month_end = _month_window(year, month)
            qs = qs.filter(start_date__lte=month_end, end_date__gte=month_start)
            source_qs = qs
        else:
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(days, 365))
            since = timezone.now() - timedelta(days=days)
            source_qs = qs.filter(applied_at__gte=since)

        totals: Dict[int, Dict[str, Any]] = {}
        for lr in source_qs.select_related("employee", "employee__user"):
            emp = getattr(lr, "employee", None)
            if not emp or not getattr(emp, "id", None):
                continue

            user = getattr(emp, "user", None)
            full_name = (
                f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
                if user
                else ""
            )
            name = (
                full_name
                or (getattr(user, "username", "") if user else "")
                or f"employee_{emp.id}"
            )

            rec = totals.get(emp.id)
            if not rec:
                rec = {
                    "employee_id": int(emp.id),
                    "name": name,
                    "total_days": 0.0,
                    "total_requests": 0,
                }
                totals[emp.id] = rec

            rec["total_requests"] = int(rec["total_requests"]) + 1
            try:
                if month_start and month_end:
                    day_contrib = _leave_days_in_window(lr, month_start, month_end)
                else:
                    day_contrib = float(lr.total_days())
                rec["total_days"] = float(rec["total_days"]) + float(day_contrib)
            except Exception:
                # If a record is malformed, skip its day contribution
                rec["total_days"] = float(rec["total_days"]) + 0.0

        rows = list(totals.values())
        rows.sort(
            key=lambda r: (
                -float(r.get("total_days", 0.0)),
                -int(r.get("total_requests", 0)),
                str(r.get("name", "")).lower(),
                int(r.get("employee_id", 0)),
            )
        )

        out: List[Dict[str, Any]] = []
        rank = 0
        prev_days = None
        for r in rows[:limit]:
            days_val = float(r.get("total_days", 0.0))
            if prev_days is None or days_val != prev_days:
                rank += 1
                prev_days = days_val

            out.append(
                {
                    "rank": int(rank),
                    "employee_id": int(r["employee_id"]),
                    "name": r.get("name") or f"employee_{r['employee_id']}",
                    "total_days": round(float(r.get("total_days", 0.0)), 1),
                    "total_requests": int(r.get("total_requests", 0)),
                }
            )

        return out
