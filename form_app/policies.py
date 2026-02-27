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


def _to_float(value, default: float) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def get_leave_policy_settings() -> LeavePolicySettings:
    settings = LeavePolicySettings.objects.order_by("-updated_at", "-id").first()
    if settings:
        return settings
    return LeavePolicySettings.objects.create()


def get_leave_policy_snapshot() -> LeavePolicySnapshot:
    settings = get_leave_policy_settings()

    field_map = {
        "VACATION": "vacation_days",
        "SICK": "sick_days",
        "MATERNITY": "maternity_days",
        "PATERNITY": "paternity_days",
        "BEREAVEMENT": "bereavement_days",
    }

    limits: dict[str, float] = {}
    for key, fallback in LEAVE_LIMITS.items():
        field = field_map.get(key)
        raw = getattr(settings, field, None) if field else None
        limits[key] = _to_float(raw, float(fallback))

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


def get_effective_probation_end_date(employee) -> Optional[date]:
    """Return the probation end date that should be used for policy calculations.

    Historically, some rows have had an incorrect persisted `probation_end_date`.
    Since probation is derived from `joining_date` and the current policy
    (`probation_period_days`), we treat the computed value as source of truth
    whenever joining_date is available.
    """

    stored = getattr(employee, "probation_end_date", None)
    joining_date = getattr(employee, "joining_date", None)
    if not joining_date:
        return stored

    try:
        expected = compute_probation_end_date(joining_date)
    except Exception:
        return stored

    if stored is None:
        return expected
    try:
        if stored < joining_date:
            return expected
    except Exception:
        return expected

    # If a persisted value exists but diverges from policy, prefer policy.
    return expected if stored != expected else stored


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
    override = getattr(employee, "leave_renewal_date_override", None)
    # Each employee has their own renewal anchor.
    # Global renewal date is intentionally NOT used here.
    anchor = _anchor_from_date(override)

    if anchor:
        anchor_month, anchor_day = anchor
    else:
        probation_end = get_effective_probation_end_date(employee)
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
