from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from django.db.models import CharField, Count, F, Value
from django.db.utils import OperationalError, ProgrammingError
from django.db.models.functions import Coalesce, Replace, Trim, Upper
from django.utils.timezone import localdate
from rest_framework import serializers

from form_app.policies import (
    get_leave_limits,
    get_leave_limits_for_employee,
    has_leave_limit_override_for_employee,
    get_carryover_percentage,
    get_leave_year_range_for_employee,
)
from form_app.models import Holiday, LeaveRequest, LeaveRequestDay

MAX_FUTURE_DAYS = 365
MAX_LEAVE_DAYS = 60


_MANUAL_USED_BY_YEAR_KEY = "_manual_used_by_year"
_MANUAL_REMAINING_BY_YEAR_KEY = "_manual_remaining_by_year"
_MANUAL_REMAINING_USED_SNAPSHOT_BY_YEAR_KEY = "_manual_remaining_used_snapshot_by_year"


def fmt_leave_days(value):
    """Format leave day counts for JSON/UI.

    - Whole numbers return as int (e.g., 5 not 5.0)
    - Half-days keep one decimal (e.g., 6.5)
    """

    if value is None:
        return None

    try:
        x = float(value)
    except (TypeError, ValueError):
        return value

    if abs(x) < 1e-9:
        x = 0.0

    if abs(x - round(x)) < 1e-9:
        return int(round(x))
    return float(round(x, 1))


def get_manual_used_by_type(*, employee, leave_year_start: date) -> dict[str, float]:
    """Return admin-specified extra used days for the given leave year.

    Stored on Employee.leave_limits_override under:
      {"_manual_used_by_year": {"YYYY-MM-DD": {"VACATION": 5, ...}}}
    """

    raw = getattr(employee, "leave_limits_override", None)
    if not isinstance(raw, dict):
        return {}

    by_year = raw.get(_MANUAL_USED_BY_YEAR_KEY)
    if not isinstance(by_year, dict):
        return {}

    key = leave_year_start.isoformat() if leave_year_start else None
    if not key:
        return {}

    used_map = by_year.get(key)
    if not isinstance(used_map, dict):
        return {}

    out: dict[str, float] = {}
    for k, v in used_map.items():
        if not isinstance(k, str):
            continue
        lt = normalize_leave_type(k)
        if not lt:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        # Keep consistent half-day rounding behavior.
        out[lt] = float(_round_to_half_day(num))

    return out


def get_manual_remaining_by_type(
    *, employee, leave_year_start: date
) -> dict[str, float]:
    """Return admin-specified direct remaining balances for the given leave year.

    Stored on Employee.leave_limits_override under:
      {"_manual_remaining_by_year": {"YYYY-MM-DD": {"VACATION": 8, ...}}}
    """

    raw = getattr(employee, "leave_limits_override", None)
    if not isinstance(raw, dict):
        return {}

    by_year = raw.get(_MANUAL_REMAINING_BY_YEAR_KEY)
    if not isinstance(by_year, dict):
        return {}

    key = leave_year_start.isoformat() if leave_year_start else None
    if not key:
        return {}

    remaining_map = by_year.get(key)
    if not isinstance(remaining_map, dict):
        return {}

    out: dict[str, float] = {}
    for k, v in remaining_map.items():
        if not isinstance(k, str):
            continue
        lt = normalize_leave_type(k)
        if not lt:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        out[lt] = float(_round_to_half_day(num))

    return out


def get_manual_remaining_used_baseline(
    *,
    employee,
    leave_year_start: date,
    leave_type: str,
    yearly_limit: float,
) -> Optional[float]:
    """Return the used-days baseline implied by an admin remaining override.

    Django admin's manual remaining balance sets the source-of-truth balance at
    the time it is saved. The saved used-days snapshot lets future approvals
    continue to deduct from that target instead of freezing the balance forever.
    """

    lt = normalize_leave_type(leave_type)
    if not lt:
        return None

    manual_remaining = get_manual_remaining_by_type(
        employee=employee, leave_year_start=leave_year_start
    ).get(lt)
    if manual_remaining is None:
        return None

    total = max(float(yearly_limit or 0.0), 0.0)
    remaining = min(max(float(manual_remaining or 0.0), 0.0), total)
    return max(total - remaining, 0.0)


def get_manual_remaining_used_snapshot_by_type(
    *, employee, leave_year_start: date
) -> dict[str, float]:
    raw = getattr(employee, "leave_limits_override", None)
    if not isinstance(raw, dict):
        return {}

    by_year = raw.get(_MANUAL_REMAINING_USED_SNAPSHOT_BY_YEAR_KEY)
    if not isinstance(by_year, dict):
        return {}

    key = leave_year_start.isoformat() if leave_year_start else None
    if not key:
        return {}

    snapshot_map = by_year.get(key)
    if not isinstance(snapshot_map, dict):
        return {}

    out: dict[str, float] = {}
    for k, v in snapshot_map.items():
        if not isinstance(k, str):
            continue
        lt = normalize_leave_type(k)
        if not lt:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        out[lt] = float(_round_to_half_day(num))

    return out


def apply_manual_remaining_used_adjustment(
    *,
    employee,
    leave_year_start: date,
    leave_type: str,
    yearly_limit: float,
    current_used: float,
) -> float:
    baseline = get_manual_remaining_used_baseline(
        employee=employee,
        leave_year_start=leave_year_start,
        leave_type=leave_type,
        yearly_limit=yearly_limit,
    )
    if baseline is None:
        return float(current_used or 0.0)

    lt = normalize_leave_type(leave_type)
    snapshot = get_manual_remaining_used_snapshot_by_type(
        employee=employee, leave_year_start=leave_year_start
    ).get(lt)
    if snapshot is None:
        snapshot = float(baseline)

    return max(float(baseline) + float(current_used or 0.0) - float(snapshot), 0.0)


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

    manual_remaining_baseline = get_manual_remaining_used_baseline(
        employee=employee,
        leave_year_start=leave_year_start,
        leave_type=leave_type,
        yearly_limit=limits[leave_type],
    )

    vacation_carry = 0.0
    if (
        manual_remaining_baseline is None
        and leave_type == "VACATION"
        and not has_leave_limit_override_for_employee(employee, "VACATION")
    ):
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(
            employee, on_date=prev_day
        )
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    manual_used = get_manual_used_by_type(
        employee=employee, leave_year_start=leave_year_start
    ).get(leave_type, 0.0)
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

    approved_used_days = sum(
        overlapping_days(lr, leave_year_start, leave_year_end_excl)
        for lr in approved_qs
    )
    used_days = apply_manual_remaining_used_adjustment(
        employee=employee,
        leave_year_start=leave_year_start,
        leave_type=leave_type,
        yearly_limit=limits[leave_type],
        current_used=float(approved_used_days) + float(manual_used),
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

    manual_remaining_baseline = get_manual_remaining_used_baseline(
        employee=employee,
        leave_year_start=leave_year_start,
        leave_type=lt,
        yearly_limit=limits[lt],
    )

    vacation_carry = 0.0
    if (
        manual_remaining_baseline is None
        and lt == "VACATION"
        and not has_leave_limit_override_for_employee(employee, "VACATION")
    ):
        prev_day = leave_year_start - timedelta(days=1)
        prev_start, prev_end_excl = get_leave_year_range_for_employee(
            employee, on_date=prev_day
        )
        vacation_carry = float(carry_forward_only(employee, prev_start, prev_end_excl))

    total_allowed = float(limits[lt]) + float(vacation_carry)
    manual_used = get_manual_used_by_type(
        employee=employee, leave_year_start=leave_year_start
    ).get(lt, 0.0)
    actual_used_before = 0.0

    status_norm = (getattr(req, "status", "") or "").strip().upper()

    approved_qs = (
        LeaveRequest.objects.filter(
            employee=employee,
            leave_type=lt,
            status="APPROVED",
            start_date__lt=leave_year_end_excl,
            end_date__gte=leave_year_start,
        )
        .annotate(_order_ts=Coalesce("approved_at", "applied_at"))
        .order_by("_order_ts", "id", "start_date", "end_date")
    )

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

        effective_used_before = apply_manual_remaining_used_adjustment(
            employee=employee,
            leave_year_start=leave_year_start,
            leave_type=lt,
            yearly_limit=limits[lt],
            current_used=float(manual_used or 0.0) + float(actual_used_before),
        )
        remaining = max(float(total_allowed) - float(effective_used_before), 0.0)
        paid_portion = min(remaining, lr_days)
        unpaid_portion = max(lr_days - paid_portion, 0.0)

        if lr.id == req.id:
            paid_for_req = paid_portion
            unpaid_for_req = unpaid_portion
            break

        actual_used_before += float(lr_days)

    if status_norm != "APPROVED":
        # Preview for a not-yet-approved request.
        effective_used_before = apply_manual_remaining_used_adjustment(
            employee=employee,
            leave_year_start=leave_year_start,
            leave_type=lt,
            yearly_limit=limits[lt],
            current_used=float(manual_used or 0.0) + float(actual_used_before),
        )
        remaining = max(float(total_allowed) - float(effective_used_before), 0.0)
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

    if isinstance(session, bool):
        return "AM" if session is True else "FULL"

    s = str(session).strip().upper()
    if s == "FD":
        s = "FULL"
    if s in {"MORNING"}:
        s = "AM"
    if s in {"AFTERNOON"}:
        s = "PM"

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
    allow_overlap_with_ids: Optional[list[int]] = None,
    dates: Optional[list[date]] = None,
) -> Tuple[str, str]:

    if not profile:
        raise serializers.ValidationError("User profile not found.")

    if profile.role == "ADMIN":
        raise serializers.ValidationError("Admins cannot apply for leave.")

    if not employee:
        raise serializers.ValidationError("Employee record not found.")

    today = localdate()

    if dates is not None:
        if not isinstance(dates, list) or not dates:
            raise serializers.ValidationError("dates must be a non-empty list.")
        if start_date is not None or end_date is not None:
            raise serializers.ValidationError(
                "Send either start_date/end_date (range) or dates[] (manual), not both."
            )
    else:
        if start_date is None or end_date is None:
            raise serializers.ValidationError("Start date and end date are required.")

    if dates is not None:
        if any(d is None for d in dates):
            raise serializers.ValidationError("dates contains an invalid value.")
        if any(d < today for d in dates):
            raise serializers.ValidationError(
                "You cannot apply leave with a date in the past."
            )
        if any(d > today + timedelta(days=MAX_FUTURE_DAYS) for d in dates):
            raise serializers.ValidationError(
                f"You cannot apply leave more than {MAX_FUTURE_DAYS} days in the future."
            )
    else:
        if start_date < today:
            raise serializers.ValidationError(
                "You cannot apply leave with a start date in the past."
            )

        if start_date > today + timedelta(days=MAX_FUTURE_DAYS):
            raise serializers.ValidationError(
                f"You cannot apply leave more than {MAX_FUTURE_DAYS} days in the future."
            )

    if dates is None and end_date < start_date:
        raise serializers.ValidationError("End date cannot be earlier than start date.")

    # Expand requested working dates (Mon-Fri). For manual mode, reject weekend selections.
    if dates is not None:
        uniq = sorted(set(dates))
        weekend = [d for d in uniq if d.weekday() >= 5]
        if weekend:
            raise serializers.ValidationError(
                "Weekend date(s) are not allowed in manual selection: "
                + ", ".join(d.isoformat() for d in weekend)
            )
        requested_dates = [d for d in uniq if d.weekday() < 5]
        start_date = min(uniq)
        end_date = max(uniq)
    else:
        # Range mode: include weekdays in the interval.
        total_days = (end_date - start_date).days + 1
        if total_days <= 0:
            raise serializers.ValidationError(
                "End date cannot be earlier than start date."
            )
        if total_days > MAX_LEAVE_DAYS:
            raise serializers.ValidationError(
                f"Leave duration cannot exceed {MAX_LEAVE_DAYS} days."
            )

        requested_dates = [
            start_date + timedelta(days=i)
            for i in range(total_days)
            if (start_date + timedelta(days=i)).weekday() < 5
        ]

    # Holiday check should match the actual requested working days.
    holiday_rows = list(
        Holiday.objects.filter(
            is_active=True,
            date__in=requested_dates,
        ).values_list("date", "name")
    )

    if holiday_rows:
        parts = [
            f"{d.isoformat()} ({n})"
            for d, n in sorted(holiday_rows, key=lambda x: x[0])
        ]
        raise serializers.ValidationError(
            "Selected date(s) are holidays: " + ", ".join(parts)
        )

    if dates is not None and len(requested_dates) > MAX_LEAVE_DAYS:
        raise serializers.ValidationError(
            f"Leave duration cannot exceed {MAX_LEAVE_DAYS} working days."
        )

    normalized_session = normalize_session(session)
    normalized_leave_type = normalize_leave_type(leave_type)

    if not requested_dates:
        raise serializers.ValidationError(
            "Selected date range has 0 working days (weekend-only). Please choose weekdays."
        )

    if not (reason or "").strip():
        raise serializers.ValidationError("Reason is required.")

    # Overlap check:

    days_qs = LeaveRequestDay.objects.annotate(
        _status_norm=_norm_status_expr("leave_request__status")
    ).filter(
        leave_request__employee=employee,
        date__in=requested_dates,
        _status_norm__in=[LeaveRequest.STATUS_PENDING, LeaveRequest.STATUS_APPROVED],
    )

    exclude_overlap_ids: set[int] = set()
    if instance_id:
        exclude_overlap_ids.add(int(instance_id))
    if allow_overlap_with_id:
        exclude_overlap_ids.add(int(allow_overlap_with_id))
    if allow_overlap_with_ids:
        for _id in allow_overlap_with_ids:
            if _id:
                exclude_overlap_ids.add(int(_id))

    if exclude_overlap_ids:
        days_qs = days_qs.exclude(leave_request__id__in=exclude_overlap_ids)

    if days_qs.exists():
        raise serializers.ValidationError(
            "You already have a leave applied for one or more selected dates."
        )

    # 2) Legacy leaves that don't have LeaveRequestDay rows yet
    legacy = (
        LeaveRequest.objects.annotate(days_count=Count("days"))
        .annotate(_status_norm=_norm_status_expr("status"))
        .filter(
            employee=employee,
            days_count=0,
            start_date__lte=end_date,
            end_date__gte=start_date,
            _status_norm__in=[
                LeaveRequest.STATUS_PENDING,
                LeaveRequest.STATUS_APPROVED,
            ],
        )
    )
    if exclude_overlap_ids:
        legacy = legacy.exclude(id__in=exclude_overlap_ids)

    # Confirm actual overlap by checking date membership against legacy ranges.
    # Use iterator() to avoid loading large querysets into memory and to avoid
    # missing overlaps due to arbitrary slicing.
    for lr in legacy.only("start_date", "end_date").iterator(chunk_size=500):
        for d in requested_dates:
            if lr.start_date <= d <= lr.end_date:
                raise serializers.ValidationError(
                    "You already have a leave applied for one or more selected dates."
                )

    # 2) Legacy leaves that don't have LeaveRequestDay rows yet
    legacy = (
        LeaveRequest.objects.annotate(days_count=Count("days"))
        .annotate(_status_norm=_norm_status_expr("status"))
        .filter(
            employee=employee,
            days_count=0,
            start_date__lte=end_date,
            end_date__gte=start_date,
            _status_norm__in=[
                LeaveRequest.STATUS_PENDING,
                LeaveRequest.STATUS_APPROVED,
            ],
        )
    )
    if exclude_overlap_ids:
        legacy = legacy.exclude(id__in=exclude_overlap_ids)

    # Confirm actual overlap by checking date membership against legacy ranges.
    # Use iterator() to avoid loading large querysets into memory and to avoid
    # missing overlaps due to arbitrary slicing.
    for lr in legacy.only("start_date", "end_date").iterator(chunk_size=500):
        for d in requested_dates:
            if lr.start_date <= d <= lr.end_date:
                raise serializers.ValidationError(
                    "You already have a leave applied for one or more selected dates."
                )

    return normalized_session, normalized_leave_type


def compute_leave_days_for_payload(*, employee, payload: dict) -> float:
    temp = LeaveRequest(employee=employee, **payload)
    return float(temp.total_days())


def count_weekdays_inclusive(start: date | None, end: date | None) -> int:
    if not start or not end or start > end:
        return 0
    cur = start
    count = 0
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def get_leave_selected_dates(req: LeaveRequest) -> tuple[list[date], bool]:
    """Return selected working dates for a leave request.

    - If LeaveRequestDay rows exist, returns those dates (sorted).
    - Otherwise, returns all weekdays in the [start_date, end_date] range.

    Also returns `is_selective`: True when LeaveRequestDay rows exist but do not
    match the full weekday range (manual/selective choice).
    """

    if (
        not req
        or not getattr(req, "start_date", None)
        or not getattr(req, "end_date", None)
    ):
        return [], False

    selected_dates: list[date] = []
    try:
        if hasattr(req, "days"):
            # Prefer prefetched related set when available.
            selected_dates = list(
                req.days.order_by("date").values_list("date", flat=True)
            )
    except (OperationalError, ProgrammingError):
        selected_dates = []
    except Exception:
        selected_dates = []

    if selected_dates:
        expected = count_weekdays_inclusive(req.start_date, req.end_date)
        is_selective = expected > 0 and len(selected_dates) != expected
        return selected_dates, bool(is_selective)

    # Fallback for legacy rows: derive weekdays from the range.
    cur = req.start_date
    while cur <= req.end_date:
        if cur.weekday() < 5:
            selected_dates.append(cur)
        cur += timedelta(days=1)
    return selected_dates, False


def _round_to_half_day(value: float) -> float:
    d = Decimal(str(value))
    half = Decimal("0.5")
    return float((d / half).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * half)


def overlapping_days(
    req: LeaveRequest, window_start: date, window_end_exclusive: date
) -> float:
    # If per-day rows exist, count only those.
    if req.pk and hasattr(req, "days"):
        try:
            day_rows = list(
                req.days.filter(
                    date__gte=window_start, date__lt=window_end_exclusive
                ).values_list("date", "session")
            )
        except (OperationalError, ProgrammingError):
            day_rows = []

        if day_rows:
            total = 0.0
            for d, sess in day_rows:
                if d.weekday() >= 5:
                    continue
                s = (sess or "FULL").strip().upper()
                if s == "FD":
                    s = "FULL"
                total += 0.5 if s in {"AM", "PM"} else 1.0
            return max(float(total), 0.0)

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
    joining_date = getattr(employee, "joining_date", None)
    # Carry-forward applies only if the employee was employed for the
    # Full previous leave year window.
    if joining_date and joining_date > prev_start:
        return 0.0

    vacation_limit = float(get_leave_limits().get("VACATION", 0.0))

    qs = LeaveRequest.objects.filter(
        employee=employee,
        leave_type="VACATION",
        status="APPROVED",
        start_date__lt=prev_end_exclusive,
        end_date__gte=prev_start,
    )
    probation_end = getattr(employee, "probation_end_date", None)
    if probation_end:
        qs = qs.filter(start_date__gt=probation_end)

    actual_used = sum(overlapping_days(lr, prev_start, prev_end_exclusive) for lr in qs)
    manual_used = get_manual_used_by_type(
        employee=employee, leave_year_start=prev_start
    ).get("VACATION", 0.0)
    effective_used = apply_manual_remaining_used_adjustment(
        employee=employee,
        leave_year_start=prev_start,
        leave_type="VACATION",
        yearly_limit=vacation_limit,
        current_used=float(actual_used) + float(manual_used or 0.0),
    )
    remaining = max(vacation_limit - float(effective_used), 0.0)

    carry_pct = max(0, min(get_carryover_percentage(), 50))
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

    # "Paid":The entire request is within remaining balance.
    return float(leave_days or 0.0) > 0.0 and unpaid_days <= 0.0
