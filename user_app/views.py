from datetime import date, datetime, timedelta
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest
from .models import Profile, Employee
from .serializers import ProfileSerializer

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    profile = Profile.objects.select_related('employee').get(user=request.user)
    serializer = ProfileSerializer(profile)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hello_dashboard(request):

    profile = Profile.objects.select_related('employee', 'user').get(user=request.user)
    employee = profile.employee

    if not employee:
        return Response(
            {"error": "Employee record not found for user"},
            status=status.HTTP_404_NOT_FOUND,
        )

    name = employee.name or profile.user.get_full_name() or profile.user.username
    is_on_probation = employee.is_on_probation()

    # Calculate leave balance for current year
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

    # Get recent 5 requests
    recent_requests = []
    for req in (
        LeaveRequest.objects.filter(employee=employee)
        .order_by('-applied_at')[:5]
    ):
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

    # Count pending requests
    pending_requests = LeaveRequest.objects.filter(
        employee=employee,
        status='PENDING',
    ).count()

    # Build response data
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

    # Calculate monthly KPIs ONLY for ADMIN
    if profile.role == 'ADMIN':
        today = date.today()
        monthly_requests = LeaveRequest.objects.filter(
            employee=employee,
            start_date__year=today.year,
            start_date__month=today.month,
        )
        response_data["monthly_kpis"] = {
            "leave_requests": monthly_requests.exclude(leave_type='WFH').count(),
            "wfh_requests": monthly_requests.filter(leave_type='WFH').count(),
        }

    return Response(response_data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_leave_balances(request):

    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response(
                {"error": "Employee record not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        approved_requests = LeaveRequest.objects.filter(
            employee=employee,
            status='APPROVED',
            start_date__year=date.today().year,
        )

        balances = []

        # ❌ Icons removed completely
        leave_config = {
            'VACATION': {
                'color': 'bg-blue-100 text-blue-600',
                'type': 'Vacation'
            },
            'SICK': {
                'color': 'bg-green-100 text-green-600',
                'type': 'Sick'
            },
            'MATERNITY': {
                'color': 'bg-pink-100 text-pink-600',
                'type': 'Maternity'
            },
            'PATERNITY': {
                'color': 'bg-indigo-100 text-indigo-600',
                'type': 'Paternity'
            },
            'BEREAVEMENT': {
                'type': 'Bereavement'
            },
        }

        for leave_type, total_allowed in LEAVE_LIMITS.items():
            used = sum(
                req.total_days()
                for req in approved_requests
                if req.leave_type == leave_type
            )

            config = leave_config.get(leave_type, {
                'type': leave_type.capitalize()
            })

            balances.append({
                "type": config['type'],
                # "color": config['color'],
                "used": used,
                "total": total_allowed,
                "remaining": max(total_allowed - used, 0)
            })

        return Response(balances)

    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_notifications(request):

    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response([], status=status.HTTP_200_OK)

        notifications = []
        
        # Get recent leave requests (last 30 days)
        recent_requests = LeaveRequest.objects.filter(
            employee=employee,
            applied_at__gte=datetime.now() - timedelta(days=30)
        ).order_by('-applied_at')

        for req in recent_requests:
            time_diff = datetime.now() - req.applied_at
            
            # Calculate time ago
            if time_diff.days == 0:
                hours = time_diff.seconds // 3600
                time_ago = f"{hours} hours ago" if hours > 0 else "Just now"
            else:
                time_ago = f"{time_diff.days} day{'s' if time_diff.days > 1 else ''} ago"
            
            # Format date range
            if req.start_date == req.end_date:
                date_range = req.start_date.strftime('%b %d')
            else:
                date_range = f"{req.start_date.strftime('%b %d')}-{req.end_date.strftime('%d')}"
            
            # Create notifications based on status
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

        return Response(notifications[:10])  # Return last 10 notifications

    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_recent_activities(request):

    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response([], status=status.HTTP_200_OK)

        activities = []
        
        # Get recent requests (last 30 days)
        recent_requests = LeaveRequest.objects.filter(
            employee=employee,
            applied_at__gte=datetime.now() - timedelta(days=30)
        ).order_by('-applied_at')[:10]

        for req in recent_requests:
            time_diff = datetime.now() - req.applied_at
            
            # Calculate time ago
            if time_diff.days == 0:
                hours = time_diff.seconds // 3600
                time_ago = f"Submitted {hours} hours ago" if hours > 0 else "Submitted just now"
            else:
                time_ago = f"Submitted {time_diff.days} days ago"
            
            # Format date range
            date_range = req.start_date.strftime('%b %d, %Y')
            if req.start_date != req.end_date:
                date_range += f" - {req.end_date.strftime('%b %d, %Y')}"
            
            # Add submission activity
            activities.append({
                "id": req.id,
                "type": "request",
                "title": f"{req.get_leave_type_display()} Request Submitted",
                "description": f"{date_range} ({req.total_days()} days) • {req.reason or 'No reason provided'}",
                "time": time_ago,
            })
            
            # Add approval/rejection activity if applicable
            if req.status == 'APPROVED':
                activities.append({
                    "id": req.id + 10000,
                    "type": "notification",
                    "title": "Leave Request Approved",
                    "description": f"Your {req.get_leave_type_display().lower()} request was reviewed and approved",
                    "time": time_ago.replace("Submitted", "Approved"),
                    "icon": "Approved",
                    "color": "text-green-600 dark:text-green-400"
                })
            
            elif req.status == 'REJECTED':
                activities.append({
                    "id": req.id + 20000,
                    "type": "notification",
                    "title": "Leave Request Rejected",
                    "description": f"Your {req.get_leave_type_display().lower()} request was rejected",
                    "time": time_ago.replace("Submitted", "Rejected"),
                    "icon": "Rejected",
                    "color": "text-red-600 dark:text-red-400"
                })

        return Response(activities[:8])  # Return last 8 activities

    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_calendar_days(request):

    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response([], status=status.HTTP_200_OK)

        # Get current month details
        today = date.today()
        year = today.year
        month = today.month
        
        # Calculate days in month and first day of month
        from calendar import monthrange
        first_weekday, days_in_month = monthrange(year, month)
        
        # Get approved leaves for current month
        month_leaves = LeaveRequest.objects.filter(
            employee=employee,
            status='APPROVED',
            start_date__year=year,
            start_date__month=month
        )

        # Build calendar days array
        days = []
        
        # Add empty days for week alignment (Sunday = 0, Monday = 1, etc.)
        for i in range(first_weekday):
            days.append({"day": None, "events": []})
        
        # Color mapping for leave types
        color_map = {
            'VACATION': 'bg-blue-500',
            'SICK': 'bg-green-500',
            'WFH': 'bg-amber-500',
            'MATERNITY': 'bg-pink-500',
            'PATERNITY': 'bg-indigo-500',
            'BEREAVEMENT': 'bg-purple-500'
        }
        
        # Add days with leave events
        for day in range(1, days_in_month + 1):
            day_events = []
            current_date = date(year, month, day)
            
            # Check if any leave falls on this day
            for leave in month_leaves:
                if leave.start_date <= current_date <= leave.end_date:
                    label = leave.get_leave_type_display().lower()
                    
                    # Add session indicator
                    if leave.session == 'AM':
                        label += " (AM)"
                    elif leave.session == 'PM':
                        label += " (PM)"
                    elif leave.total_days() == 0.5:
                        label += " (½)"
                    
                    day_events.append({
                        "day": day,
                        "type": leave.leave_type.lower(),
                        "label": label,
                        "color": color_map.get(leave.leave_type, 'bg-gray-500')
                    })
            
            days.append({"day": day, "events": day_events})

        return Response(days)

    except Profile.DoesNotExist:
        return Response([], status=status.HTTP_200_OK)