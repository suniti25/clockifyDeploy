from datetime import date, datetime, timedelta
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

# USER PROFILE
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = ProfileSerializer(profile)
    return Response(serializer.data, status=status.HTTP_200_OK)

# DASHBOARD
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hello_dashboard(request):
    try:
        profile = Profile.objects.select_related('employee', 'user').get(user=request.user)
    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    employee = profile.employee
    if not employee:
        return Response(
            {"error": "Employee record not found for user"},
            status=status.HTTP_404_NOT_FOUND
        )

    name = employee.name or profile.user.get_full_name() or profile.user.username
    is_on_probation = employee.is_on_probation()

    approved_requests = LeaveRequest.objects.filter(
        employee=employee,
        status='APPROVED',
        start_date__year=date.today().year,
    )

    leave_balance = {}
    for leave_type, total_allowed in LEAVE_LIMITS.items():
        used = sum(
            req.total_days()
            for req in approved_requests
            if req.leave_type == leave_type
        )
        leave_balance[leave_type.lower()] = {
            "total": total_allowed,
            "used": used,
            "remaining": max(total_allowed - used, 0),
        }

    recent_requests = []
    for req in LeaveRequest.objects.filter(employee=employee).order_by('-applied_at')[:5]:
        entry = {
            "id": req.id,
            "type": "WFH" if req.leave_type == 'WFH' else "LEAVE",
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "status": req.status,
        }
        if req.leave_type != 'WFH':
            entry["leave_type"] = req.get_leave_type_display()
        recent_requests.append(entry)

    pending_requests = LeaveRequest.objects.filter(
        employee=employee,
        status='PENDING'
    ).count()

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

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_leave_balances(request):
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    approved_requests = LeaveRequest.objects.filter(
        employee=employee,
        status='APPROVED',
        start_date__year=date.today().year,
    )

    balances = []
    leave_config = {
        'VACATION': 'Vacation',
        'SICK': 'Sick',
        'MATERNITY': 'Maternity',
        'PATERNITY': 'Paternity',
        'BEREAVEMENT': 'Bereavement',
    }

    for leave_type, total_allowed in LEAVE_LIMITS.items():
        used = sum(
            req.total_days()
            for req in approved_requests
            if req.leave_type == leave_type
        )

        balances.append({
            "type": leave_config.get(leave_type, leave_type.capitalize()),
            "used": used,
            "total": total_allowed,
            "remaining": max(total_allowed - used, 0),
        })

    return Response(balances, status=status.HTTP_200_OK)

# NOTIFICATIONS

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_notifications(request):
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    notifications = []
    now = timezone.now()

    recent_requests = LeaveRequest.objects.filter(
        employee=employee,
        applied_at__gte=now - timedelta(days=30)
    ).order_by('-applied_at')

    for req in recent_requests:
        time_diff = now - req.applied_at

        if time_diff.days == 0:
            hours = time_diff.seconds // 3600
            time_ago = f"{hours} hours ago" if hours > 0 else "Just now"
        else:
            time_ago = f"{time_diff.days} day{'s' if time_diff.days > 1 else ''} ago"

        date_range = (
            req.start_date.strftime('%b %d')
            if req.start_date == req.end_date
            else f"{req.start_date.strftime('%b %d')}-{req.end_date.strftime('%d')}"
        )

        if req.status == 'APPROVED':
            notifications.append({
                "id": req.id,
                "title": "Leave Approved",
                "message": f"Your {req.get_leave_type_display().lower()} request for {date_range} has been approved",
                "time": time_ago,
                "read": False
            })

        elif req.status == 'REJECTED':
            notifications.append({
                "id": req.id + 2000,
                "title": "Leave Rejected",
                "message": f"Your {req.get_leave_type_display().lower()} request for {date_range} was rejected",
                "time": time_ago,
                "read": False
            })

        elif req.status == 'PENDING':
            notifications.append({
                "id": req.id + 1000,
                "title": f"{req.get_leave_type_display()} Request Pending",
                "message": f"Your {req.get_leave_type_display().lower()} request is pending manager approval",
                "time": time_ago,
                "read": True
            })

    return Response(notifications[:10], status=status.HTTP_200_OK)

# RECENT ACTIVITIES
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_recent_activities(request):
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    activities = []
    now = timezone.now()  

    recent_requests = LeaveRequest.objects.filter(
        employee=employee,
        applied_at__gte=now - timedelta(days=30)  
    ).order_by('-applied_at')[:10]

    for req in recent_requests:
        date_range = req.start_date.strftime('%b %d, %Y')
        if req.start_date != req.end_date:
            date_range += f" - {req.end_date.strftime('%b %d, %Y')}"

        activities.append({
            "id": req.id,
            "type": "request",
            "title": f"{req.get_leave_type_display()} Request Submitted",
            "description": f"{date_range} ({req.total_days()} days)",
            "time": "Recently",
        })

    return Response(activities[:8], status=status.HTTP_200_OK)


# CALENDAR DAYS

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_calendar_days(request):
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

    employee = profile.employee
    if not employee:
        return Response([], status=status.HTTP_200_OK)

    today = date.today()
    year, month = today.year, today.month
    first_weekday, days_in_month = monthrange(year, month)

    month_leaves = LeaveRequest.objects.filter(
        employee=employee,
        status='APPROVED',
        start_date__year=year,
        start_date__month=month
    )

    days = [{"day": None, "events": []} for _ in range(first_weekday)]

    for day in range(1, days_in_month + 1):
        current_date = date(year, month, day)
        events = []

        for leave in month_leaves:
            if leave.start_date <= current_date <= leave.end_date:
                events.append({
                    "day": day,
                    "type": leave.leave_type.lower(),
                })

        days.append({"day": day, "events": events})

    return Response(days, status=status.HTTP_200_OK)
