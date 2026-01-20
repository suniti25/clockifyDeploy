from datetime import date, timedelta
from calendar import monthrange

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest
from .models import Profile
from .serializers import ProfileSerializer

# Leave year

def year_reset(year: int, month: int, day: int) -> date:
    """
  Handles leap years also.
    """
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def get_leave_year_range(probation_end_date: date, today: date):
    """
    Leave year is anchored to probation_end_date (month/day).
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
    Carry forward is ONLY for VACATION.
    """
    yearly_vacation = LEAVE_LIMITS.get("VACATION", 0)

    prev_used = sum(
        lr.total_days()
        for lr in LeaveRequest.objects.filter(
            employee=employee,
            leave_type="VACATION",
            status="APPROVED",
            is_paid=True,
            start_date__gte=prev_start,
            start_date__lt=prev_end,
        )
    )

    prev_remaining = max(yearly_vacation - prev_used, 0)
    carry = prev_remaining * 0.5

    # keep half-day support
    return round(carry, 1)


def _approved_requests_in_window(employee, window_start: date, window_end_exclusive: date):
    """
    Fetch approved leaves that overlap the leave-year window.
    window_end_exclusive is exclusive.
    """
    window_end_inclusive = window_end_exclusive - timedelta(days=1)

    return LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        # overlap condition:
        start_date__lte=window_end_inclusive,
        end_date__gte=window_start,
    )


def _group_used_by_type(approved_requests):
    """
    Single-pass grouping for used days per leave_type.
    """
    used_by_type = {}
    for req in approved_requests:
        used_by_type[req.leave_type] = used_by_type.get(req.leave_type, 0) + req.total_days()
    return used_by_type

# USER PROFILE
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = ProfileSerializer(profile)
    return Response(serializer.data, status=status.HTTP_200_OK)

# DASHBOARD
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def hello_dashboard(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    employee = profile.employee
    if not employee:
        return Response(
            {"error": "Employee record not found for user"},
            status=status.HTTP_404_NOT_FOUND,
        )

    name = employee.name or request.user.get_full_name() or request.user.username
    is_on_probation = employee.is_on_probation()
    today = date.today()

    # Leave year window based on probation end date
    leave_year_start, leave_year_end = get_leave_year_range(employee.probation_end_date, today)

    # Previous leave year window
    prev_day = leave_year_start - timedelta(days=1)
    prev_start, prev_end = get_leave_year_range(employee.probation_end_date, prev_day)

    # Carry forward only after probation ends
    vacation_carry = carry_forward_only(employee, prev_start, prev_end) if not is_on_probation else 0

    approved_requests = _approved_requests_in_window(employee, leave_year_start, leave_year_end)
    used_by_type = _group_used_by_type(approved_requests)

    leave_balance = {}
    for leave_type, yearly_limit in LEAVE_LIMITS.items():
        used = used_by_type.get(leave_type, 0)

        total_allowed = yearly_limit
        carry_forward = 0

        # Only Vacation gets carry forward
        if leave_type == "VACATION":
            carry_forward = vacation_carry
            total_allowed = yearly_limit + vacation_carry

        leave_balance[leave_type.lower()] = {
            "leave_year_start": leave_year_start.isoformat(),
            "leave_year_end": (leave_year_end - timedelta(days=1)).isoformat(),
            "carry_forward": carry_forward,
            "total": total_allowed,
            "used": used,
            "remaining": max(total_allowed - used, 0),
        }

    recent_requests = []
    for req in LeaveRequest.objects.filter(employee=employee).order_by("-applied_at")[:5]:
        entry = {
            "id": req.id,
            "type": "WFH" if req.leave_type == "WFH" else "LEAVE",
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "status": req.status,
        }
        if req.leave_type != "WFH":
            entry["leave_type"] = req.get_leave_type_display()
        recent_requests.append(entry)

    pending_requests = LeaveRequest.objects.filter(employee=employee, status="PENDING").count()

    response_data = {
        "message": "Dashboard data retrieved successfully",
        "name": name,
        "role": profile.role,
        "joining_date": employee.joining_date.isoformat(),
        "probation_end_date": employee.probation_end_date.isoformat(),
        "is_on_probation": is_on_probation,
        "pending_requests": pending_requests,
        "leave_balance": leave_balance,
        "recent_requests": recent_requests,
    }

    return Response(response_data, status=status.HTTP_200_OK)

# LEAVE BALANCES
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_leave_balances(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    today = date.today()
    is_on_probation = employee.is_on_probation()

    leave_year_start, leave_year_end = get_leave_year_range(employee.probation_end_date, today)

    prev_day = leave_year_start - timedelta(days=1)
    prev_start, prev_end = get_leave_year_range(employee.probation_end_date, prev_day)

    vacation_carry = carry_forward_only(employee, prev_start, prev_end) if not is_on_probation else 0

    approved_requests = _approved_requests_in_window(employee, leave_year_start, leave_year_end)
    used_by_type = _group_used_by_type(approved_requests)

    leave_config = {
        "VACATION": "Vacation",
        "SICK": "Sick",
        "MATERNITY": "Maternity",
        "PATERNITY": "Paternity",
        "BEREAVEMENT": "Bereavement",
    }

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
                "type": leave_config.get(leave_type, leave_type.capitalize()),
                "leave_year_start": leave_year_start.isoformat(),
                "leave_year_end": (leave_year_end - timedelta(days=1)).isoformat(),
                "carry_forward": carry_forward,
                "used": used,
                "total": total_allowed,
                "remaining": max(total_allowed - used, 0),
            }
        )

    return Response(balances, status=status.HTTP_200_OK)

# NOTIFICATIONS
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_notifications(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    notifications = []
    now = timezone.now()

    recent_requests = LeaveRequest.objects.filter(
        employee=employee,
        applied_at__gte=now - timedelta(days=30),
    ).order_by("-applied_at")

    for req in recent_requests:
        time_diff = now - req.applied_at

        if time_diff.days == 0:
            hours = time_diff.seconds // 3600
            time_ago = f"{hours} hours ago" if hours > 0 else "Just now"
        else:
            time_ago = f"{time_diff.days} day{'s' if time_diff.days > 1 else ''} ago"

        date_range = (
            req.start_date.strftime("%b %d")
            if req.start_date == req.end_date
            else f"{req.start_date.strftime('%b %d')}-{req.end_date.strftime('%d')}"
        )

        if req.status == "APPROVED":
            notifications.append(
                {
                    "id": req.id,
                    "title": "Leave Approved",
                    "message": f"Your {req.get_leave_type_display().lower()} request for {date_range} has been approved",
                    "time": time_ago,
                    "read": False,
                }
            )
        elif req.status == "REJECTED":
            notifications.append(
                {
                    "id": req.id + 2000,
                    "title": "Leave Rejected",
                    "message": f"Your {req.get_leave_type_display().lower()} request for {date_range} was rejected",
                    "time": time_ago,
                    "read": False,
                }
            )
        elif req.status == "PENDING":
            notifications.append(
                {
                    "id": req.id + 1000,
                    "title": f"{req.get_leave_type_display()} Request Pending",
                    "message": f"Your {req.get_leave_type_display().lower()} request is pending manager approval",
                    "time": time_ago,
                    "read": True,
                }
            )

    return Response(notifications[:10], status=status.HTTP_200_OK)

# RECENT ACTIVITIES
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

    now = timezone.now()

    recent_requests = LeaveRequest.objects.filter(
        employee=employee,
        applied_at__gte=now - timedelta(days=30),
    ).order_by("-applied_at")[:10]

    activities = []
    for req in recent_requests:
        date_range = req.start_date.strftime("%b %d, %Y")
        if req.start_date != req.end_date:
            date_range += f" - {req.end_date.strftime('%b %d, %Y')}"

        activities.append(
            {
                "id": req.id,
                "type": "request",
                "title": f"{req.get_leave_type_display()} Request Submitted",
                "description": f"{date_range} ({req.total_days()} days)",
                "time": "Recently",
            }
        )

    return Response(activities[:8], status=status.HTTP_200_OK)

# CALENDAR DAYS
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_calendar_days(request):
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    today = date.today()
    year, month = today.year, today.month

    first_weekday, days_in_month = monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    # IMPORTANT: overlap with the month window (not only start_date month)
    month_leaves = LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        start_date__lte=month_end,
        end_date__gte=month_start,
    )

    days = [{"day": None, "events": []} for _ in range(first_weekday)]

    for day in range(1, days_in_month + 1):
        current_date = date(year, month, day)
        events = []

        for leave in month_leaves:
            if leave.start_date <= current_date <= leave.end_date:
                events.append(
                    {
                        "day": day,
                        "type": leave.leave_type.lower(),
                    }
                )

        days.append({"day": day, "events": events})

    return Response(days, status=status.HTTP_200_OK)
