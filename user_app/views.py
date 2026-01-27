from datetime import date, timedelta
from calendar import monthrange
from collections import defaultdict

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest
from .models import Profile
from .serializers import ProfileSerializer

from .helpers import (get_leave_year_range,carry_forward_only,approved_requests_in_window,group_used_by_type,history_queryset,
serialize_history_item, apply_history_filters,)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    return Response(ProfileSerializer(profile).data, status=status.HTTP_200_OK)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def hello_dashboard(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    employee = profile.employee
    if not employee:
        return Response({"error": "Employee record not found"}, status=status.HTTP_404_NOT_FOUND)

    today = timezone.localdate()
    is_on_probation = employee.is_on_probation()

    leave_year_start, leave_year_end = get_leave_year_range(employee.probation_end_date, today)

    prev_day = leave_year_start - timedelta(days=1)
    prev_start, prev_end = get_leave_year_range(employee.probation_end_date, prev_day)

    vacation_carry = carry_forward_only(employee, prev_start, prev_end) if not is_on_probation else 0

    approved = approved_requests_in_window(employee, leave_year_start, leave_year_end)
    used_by_type = group_used_by_type(approved, leave_year_start, leave_year_end)


    leave_balance = {}
    for leave_type, yearly_limit in LEAVE_LIMITS.items():
        used = used_by_type.get(leave_type, 0)

        total_allowed = yearly_limit
        carry_forward = 0

        if leave_type == "VACATION":
            carry_forward = vacation_carry
            total_allowed = yearly_limit + vacation_carry

        leave_balance[leave_type.lower()] = {
            "leave_year_start": leave_year_start.isoformat(),
            "leave_year_end": (leave_year_end - timedelta(days=1)).isoformat(),
            "carry_forward": carry_forward,
            "total": total_allowed,
            "used": used,
            "remaining": round(max(total_allowed - used, 0), 1),
        }

    recent_requests = [serialize_history_item(req) for req in history_queryset(employee)[:4]]

    pending_requests = LeaveRequest.objects.filter(employee=employee, status="PENDING").count()

    return Response(
        {
            "message": "Dashboard data retrieved successfully",
            "name": employee.name or request.user.get_full_name() or request.user.username,
            "role": profile.role,
            "joining_date": employee.joining_date.isoformat(),
            "probation_end_date": employee.probation_end_date.isoformat(),
            "is_on_probation": is_on_probation,
            "pending_requests": pending_requests,
            "leave_balance": leave_balance,
            "recent_requests": recent_requests,
        },
        status=status.HTTP_200_OK,
    )
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_leave_balances(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    today = timezone.localdate()
    is_on_probation = employee.is_on_probation()

    leave_year_start, leave_year_end = get_leave_year_range(employee.probation_end_date, today)

    prev_day = leave_year_start - timedelta(days=1)
    prev_start, prev_end = get_leave_year_range(employee.probation_end_date, prev_day)

    vacation_carry = carry_forward_only(employee, prev_start, prev_end) if not is_on_probation else 0

    approved = approved_requests_in_window(employee, leave_year_start, leave_year_end)
    used_by_type = group_used_by_type(approved, leave_year_start, leave_year_end)


    balances = []
    for leave_type, yearly_limit in LEAVE_LIMITS.items():
        used = used_by_type.get(leave_type, 0)

        total_allowed = yearly_limit
        carry_forward = 0

        if leave_type == "VACATION":
            carry_forward = vacation_carry
            total_allowed = yearly_limit + vacation_carry

        balances.append(
            {
                "type": leave_type.capitalize(),
                "leave_year_start": leave_year_start.isoformat(),
                "leave_year_end": (leave_year_end - timedelta(days=1)).isoformat(),
                "carry_forward": carry_forward,
                "used": used,
                "total": total_allowed,
                "remaining": round(max(total_allowed - used, 0), 1),
            }
        )

    return Response(balances, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_history(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"count": 0, "next": None, "previous": None, "results": []}, status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response({"count": 0, "next": None, "previous": None, "results": []}, status=status.HTTP_200_OK)

    # pagination
    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", 10))
    except ValueError:
        page, page_size = 1, 10

    page = max(page, 1)
    page_size = min(max(page_size, 1), 50)

    #  frontend filters
    search = request.GET.get("search", "")
    month = request.GET.get("month", "")
    leave_type = request.GET.get("type", "")       
    status_filter = request.GET.get("status", "")  

    qs = history_queryset(employee)

    #  APPLY FILTERS HERE
    qs = apply_history_filters(
        qs,
        search=search,
        month=month,
        leave_type=leave_type,
        status_filter=status_filter,
    )

    total = qs.count()

    start = (page - 1) * page_size
    end = start + page_size

    results = [serialize_history_item(req) for req in qs[start:end]]

    return Response(
        {
            "count": total,
            "page_size": page_size,  #  your frontend uses this
            "next": page + 1 if end < total else None,
            "previous": page - 1 if page > 1 else None,
            "results": results,
        },
        status=status.HTTP_200_OK,
    )



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_recent_activities(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    qs = history_queryset(employee)[:4]
    return Response([serialize_history_item(req) for req in qs], status=status.HTTP_200_OK)



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_calendar_days(request):
    """
    - Protected endpoint (IsAuthenticated)
    - Uses year/month query params
    - Only shows APPROVED leaves for the logged-in employee
    - Returns calendar grid format:
      [{day: null, events: []}, ..., {day: 1, events:[...]}, ...]
    """
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    today = timezone.localdate()
    year_str = request.GET.get("year")
    month_str = request.GET.get("month")

    try:
        year = int(year_str) if year_str else today.year
        month = int(month_str) if month_str else today.month
    except ValueError:
        return Response(
            {"error": "Invalid year/month. Use numbers like ?year=2027&month=5"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if month < 1 or month > 12:
        return Response(
            {"error": "Invalid month. Must be between 1 and 12."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    first_weekday, days_in_month = monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    # Only fetch what we need
    month_leaves = (
        LeaveRequest.objects.filter(
            employee=employee,
            status="APPROVED",
            start_date__lte=month_end,
            end_date__gte=month_start,
        )
        .only("start_date", "end_date", "leave_type")
    )

    # Precompute day -> events
    event_map = defaultdict(list)

    for leave in month_leaves:
        current = max(leave.start_date, month_start)
        last = min(leave.end_date, month_end)

        while current <= last:
            event_map[current.day].append(
                {
                    "day": current.day,
                    "type": leave.leave_type.lower(),
                }
            )
            current += timedelta(days=1)

    # Build calendar grid (leading blanks then 1..days_in_month)
    days = [{"day": None, "events": []} for _ in range(first_weekday)]

    for day_num in range(1, days_in_month + 1):
        days.append({"day": day_num, "events": event_map.get(day_num, [])})

    return Response(days, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_upcoming_leaves(request):
    """
    Returns ONLY approved leaves that are upcoming (start_date >= today)
    for the logged-in employee.
    """
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)
    # limit
    try:
        limit = int(request.GET.get("limit", 5))
    except ValueError:
        limit = 5
    limit = min(max(limit, 1), 20)

    today = timezone.localdate()

    qs = (
        LeaveRequest.objects.filter(
            employee=employee,
            status="APPROVED",          
            start_date__gte=today,      
        )
        .order_by("start_date")
        .only("id", "leave_type", "start_date", "end_date", "status","session")
    )[:limit]

    results = []
    for req in qs:
        results.append(
            {
                "id": str(req.id),
                "leave_type": req.get_leave_type_display(),
                "start_date": req.start_date.isoformat(),
                "end_date": req.end_date.isoformat(),
                "days": req.total_days(),   
                "status": req.status,       
                "message": f"{req.get_leave_type_display()} leave upcoming",
            }
        )

    return Response(results, status=status.HTTP_200_OK)