from __future__ import annotations

import logging
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from math import ceil

from django.db.models import Count
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from form_app.models import LeaveRequest

from .helpers import (
    _aggregate_approved_usage,
    _build_leave_year_context,
    _get_profile_and_employee,
    _iso,
    _leave_balance_list,
    _norm_status_expr,
    _serialize_recent,
    _serialize_upcoming,
    apply_history_filters,
    history_queryset,
)
from .serializers import ProfileSerializer

logger = logging.getLogger(__name__)


@extend_schema(
    description="Get current logged-in user's profile.",
    responses={200: ProfileSerializer},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    profile, _, err = _get_profile_and_employee(request)
    if err:
        return err
    return Response(ProfileSerializer(profile).data, status=status.HTTP_200_OK)


@extend_schema(
    description="Dashboard data: leave balances, upcoming leaves, and recent requests.",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def hello_dashboard(request):
    profile, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    ctx = _build_leave_year_context(employee)
    paid_used_by_type, probation_leave_total, unpaid_leave_total = (
        _aggregate_approved_usage(employee, ctx)
    )
    leave_balances = _leave_balance_list(
        employee, ctx, paid_used_by_type, probation_leave_total, unpaid_leave_total
    )

    today = timezone.localdate()
    upcoming_qs = (
        LeaveRequest.objects.filter(
            employee=employee, status="APPROVED", start_date__gte=today
        )
        .order_by("start_date", "end_date", "id")
        .only(
            "id",
            "leave_type",
            "start_date",
            "end_date",
            "status",
            "session",
            "start_session",
            "end_session",
        )
    )[:5]
    upcoming_leaves = [_serialize_upcoming(req) for req in upcoming_qs]

    recent_qs = history_queryset(employee)[:10]
    recent_requests = [_serialize_recent(req) for req in recent_qs]

    return Response(
        {
            "message": "Dashboard data retrieved successfully",
            "name": employee.name
            or request.user.get_full_name()
            or request.user.username,
            "role": profile.role,
            "joining_date": _iso(getattr(employee, "joining_date", None)),
            "probation_end_date": _iso(getattr(employee, "probation_end_date", None)),
            "is_on_probation": ctx.is_on_probation,
            "leave-balances": leave_balances,
            "upcoming-leaves": upcoming_leaves,
            "recent_requests": recent_requests,
        },
        status=status.HTTP_200_OK,
    )


@extend_schema(
    description="Get leave balances for current user.",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_leave_balances(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    ctx = _build_leave_year_context(employee)
    paid_used_by_type, probation_leave_total, unpaid_leave_total = (
        _aggregate_approved_usage(employee, ctx)
    )

    return Response(
        _leave_balance_list(
            employee, ctx, paid_used_by_type, probation_leave_total, unpaid_leave_total
        ),
        status=status.HTTP_200_OK,
    )


@extend_schema(
    description="Get leave request history with paging + filters.",
    parameters=[
        OpenApiParameter(
            name="page",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="page_size",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="search",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="month",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="status",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
    ],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_history(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    params = request.GET

    def _int_param(key: str, default: int) -> int:
        raw = params.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    page = max(_int_param("page", 1), 1)
    page_size = min(max(_int_param("page_size", 10), 1), 50)

    search = params.get("search", "") or ""
    month = params.get("month", "") or ""

    leave_type = ""
    type_vals = params.getlist("type")
    if type_vals:
        allowed_types: set[str] = set()
        try:
            field = LeaveRequest._meta.get_field("leave_type")
            allowed_types = {k for k, _ in (field.choices or [])}
        except Exception:
            allowed_types = {
                k for k, _ in getattr(LeaveRequest, "LEAVE_TYPE_CHOICES", [])
            }

        for raw in type_vals:
            cand = (raw or "").strip().upper()
            if cand in allowed_types:
                leave_type = cand
                break

        if not leave_type:
            leave_type = (params.get("type", "") or "").strip().upper()

    status_filter = params.get("status", "") or ""

    base_qs = history_queryset(employee)

    filtered_qs = apply_history_filters(
        base_qs,
        search=search,
        month=month,
        leave_type=leave_type,
        status_filter=status_filter,
    ).order_by("-applied_at", "-id")

    base_total = base_qs.count()
    filtered_total = filtered_qs.count()
    total_pages = ceil(filtered_total / page_size) if filtered_total else 0

    normalized = base_qs.order_by().annotate(s=_norm_status_expr("status"))
    counts_by_status = dict(
        normalized.values("s").annotate(c=Count("id")).values_list("s", "c")
    )

    approved_count = int(counts_by_status.get("APPROVED", 0))
    pending_count = int(counts_by_status.get("PENDING", 0))
    rejected_count = int(counts_by_status.get("REJECTED", 0))

    start = (page - 1) * page_size
    end = start + page_size
    page_qs = filtered_qs[start:end]

    results = [_serialize_recent(req) for req in page_qs]

    return Response(
        {
            "count": base_total,
            "filtered_count": filtered_total,
            "page_size": page_size,
            "next": page + 1 if end < filtered_total else None,
            "previous": page - 1 if page > 1 else None,
            "total_pages": total_pages,
            "current_page": page,
            "approved_count": approved_count,
            "pending_count": pending_count,
            "rejected_count": rejected_count,
            "results": results,
        },
        status=status.HTTP_200_OK,
    )


@extend_schema(
    description="Get recent leave activities (last 4).",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_recent_activities(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return Response([], status=status.HTTP_200_OK)

    qs = history_queryset(employee)[:4]
    return Response([_serialize_recent(req) for req in qs], status=status.HTTP_200_OK)


@extend_schema(
    description="Get upcoming approved leaves (default limit=5).",
    parameters=[
        OpenApiParameter(
            name="limit",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
    ],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_upcoming_leaves(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return Response([], status=status.HTTP_200_OK)

    try:
        limit = int(request.GET.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 20)

    today = timezone.localdate()
    qs = (
        LeaveRequest.objects.filter(
            employee=employee, status="APPROVED", start_date__gte=today
        )
        .order_by("start_date", "end_date", "id")
        .only("id", "leave_type", "start_date", "end_date", "status", "session")
    )[:limit]

    return Response([_serialize_upcoming(req) for req in qs], status=status.HTTP_200_OK)


@extend_schema(
    description="Calendar month view: returns array of day objects with events.",
    parameters=[
        OpenApiParameter(
            name="year",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
        OpenApiParameter(
            name="month",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
    ],
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_calendar_days(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return Response([], status=status.HTTP_200_OK)

    today = timezone.localdate()
    year_str = request.GET.get("year")
    month_str = request.GET.get("month")

    try:
        year = int(year_str) if year_str else today.year
        month = int(month_str) if month_str else today.month
    except (TypeError, ValueError):
        return Response(
            {"error": "Invalid year/month. Use numbers like ?year=2027&month=5"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if month < 1 or month > 12:
        return Response(
            {"error": "Invalid month. Must be between 1 and 12."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    _, days_in_month = monthrange(year, month)

    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    first_weekday = (month_start.weekday() + 1) % 7

    month_leaves = LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        start_date__lte=month_end,
        end_date__gte=month_start,
    ).only(
        "start_date",
        "end_date",
        "leave_type",
        "session",
        "start_session",
        "end_session",
    )

    event_map: dict[int, list[dict]] = defaultdict(list)

    for leave in month_leaves:
        current = max(leave.start_date, month_start)
        last = min(leave.end_date, month_end)

        base_session = (getattr(leave, "session", "FULL") or "FULL").strip().upper()
        start_sess = (
            (getattr(leave, "start_session", base_session) or base_session)
            .strip()
            .upper()
        )
        end_sess = (
            (getattr(leave, "end_session", base_session) or base_session)
            .strip()
            .upper()
        )

        while current <= last:
            if current.weekday() < 5:
                day_session = base_session
                if leave.start_date != leave.end_date:
                    if current == leave.start_date:
                        day_session = start_sess
                    elif current == leave.end_date:
                        day_session = end_sess

                is_half_day = day_session in {"AM", "PM"}
                leave_type_label = (
                    leave.get_leave_type_display()
                    if hasattr(leave, "get_leave_type_display")
                    else (leave.leave_type or "")
                )

                title = (
                    f"Half day {leave_type_label} Leave ({day_session})"
                    if is_half_day
                    else f"{leave_type_label} Leave"
                )

                event_map[current.day].append(
                    {
                        "day": current.day,
                        "type": (leave.leave_type or "").lower(),
                        "title": title,
                        "session": day_session,
                        "is_half_day": bool(is_half_day),
                    }
                )

            current += timedelta(days=1)

    days = [{"day": None, "events": []} for _ in range(first_weekday)]
    for day_num in range(1, days_in_month + 1):
        days.append({"day": day_num, "events": event_map.get(day_num, [])})

    return Response(days, status=status.HTTP_200_OK)
