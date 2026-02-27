from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from django.utils import timezone

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeavePolicySettings


@dataclass(frozen=True)
class LeavePolicySnapshot:
    global_renewal_date: Optional[date]
    carryover_percentage: int
    probation_period_days: int
    limits: dict[str, float]


def _to_int(value: Optional[int], default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def get_leave_policy_settings() -> LeavePolicySettings:
    settings = LeavePolicySettings.objects.order_by("-updated_at", "-id").first()
    if settings:
        return settings
    return LeavePolicySettings.objects.create()


def get_leave_policy_snapshot() -> LeavePolicySnapshot:
    settings = get_leave_policy_settings()
    limits = {
        "VACATION": float(settings.vacation_days),
        "SICK": float(settings.sick_days),
        "MATERNITY": float(settings.maternity_days),
        "PATERNITY": float(settings.paternity_days),
        "BEREAVEMENT": float(settings.bereavement_days),
    }

    # Backfill from defaults if any field is null
    for key, fallback in LEAVE_LIMITS.items():
        if key not in limits or limits[key] is None:
            limits[key] = float(fallback)

    return LeavePolicySnapshot(
        global_renewal_date=settings.global_renewal_date,
        carryover_percentage=_to_int(settings.carryover_percentage, 50),
        probation_period_days=_to_int(settings.probation_period_days, 90),
        limits=limits,
    )


def get_leave_limits() -> dict[str, float]:
    return get_leave_policy_snapshot().limits


def get_leave_limits_for_employee(employee) -> dict[str, float]:
    """Return leave limits for an employee, applying per-employee overrides."""

    limits = dict(get_leave_limits())

    raw = getattr(employee, "leave_limits_override", None)
    if not isinstance(raw, dict):
        return limits

    allowed = set(limits.keys())
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        key = k.strip().upper()
        if key not in allowed:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num < 0:
            continue
        limits[key] = num

    return limits


def get_carryover_percentage() -> int:
    return get_leave_policy_snapshot().carryover_percentage


def get_probation_days() -> int:
    return get_leave_policy_snapshot().probation_period_days


def compute_probation_end_date(joining_date: date) -> date:

    days = int(get_probation_days())
    if days <= 0:
        return joining_date - timedelta(days=1)
    return joining_date + timedelta(days=days - 1)


def _year_reset(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def _anchor_from_date(d: Optional[date]) -> Optional[tuple[int, int]]:
    if not d:
        return None
    return d.month, d.day


def get_leave_year_range_for_employee(
    employee, on_date: Optional[date] = None
) -> tuple[date, date]:
    on_date = on_date or timezone.localdate()
    policy = get_leave_policy_snapshot()

    override = getattr(employee, "leave_renewal_date_override", None)
    anchor = _anchor_from_date(override) or _anchor_from_date(
        policy.global_renewal_date
    )

    if anchor:
        anchor_month, anchor_day = anchor
    else:
        probation_end = getattr(employee, "probation_end_date", None)
        if not probation_end:
            start = date(on_date.year, 1, 1)
            end_excl = date(on_date.year + 1, 1, 1)
            return start, end_excl
        anchor_date = probation_end + timedelta(days=1)
        anchor_month, anchor_day = anchor_date.month, anchor_date.day

    start_this_year = _year_reset(on_date.year, anchor_month, anchor_day)
    if on_date >= start_this_year:
        start = start_this_year
        end_excl = _year_reset(on_date.year + 1, anchor_month, anchor_day)
    else:
        start = _year_reset(on_date.year - 1, anchor_month, anchor_day)
        end_excl = start_this_year

    return start, end_excl


def get_next_renewal_date(employee, on_date: Optional[date] = None) -> date:
    on_date = on_date or timezone.localdate()
    _, end_excl = get_leave_year_range_for_employee(employee, on_date=on_date)
    return end_excl
