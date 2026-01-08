from datetime import date

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from form_app.constants import LEAVE_LIMITS
from form_app.models import LeaveRequest
from .models import Profile
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

    today = date.today()
    monthly_requests = LeaveRequest.objects.filter(
        employee=employee,
        start_date__year=today.year,
        start_date__month=today.month,
    )
    monthly_kpis = {
        "leave_requests": monthly_requests.exclude(leave_type='WFH').count(),
        "wfh_requests": monthly_requests.filter(leave_type='WFH').count(),
    }

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

    return Response({
        "message": "Dashboard data retrieved successfully",
        "name": name,
        "role": profile.role,
        "joining_date": employee.joining_date.isoformat(),
        "probation_end_date": employee.probation_end_date.isoformat(),
        "is_on_probation": is_on_probation,
        "pending_requests": pending_requests,
        "leave_balance": leave_balance,
        "monthly_kpis": monthly_kpis,
        "recent_requests": recent_requests,
    })