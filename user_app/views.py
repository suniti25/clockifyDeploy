from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from datetime import date
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
    profile = Profile.objects.select_related('employee').get(user=request.user)
    employee = profile.employee

    name = employee.name if employee else request.user.username
    
    # Check probation status
    is_on_probation = employee.is_on_probation() if employee else False
    
    # Calculate leave balances (for employees who completed probation)
    if employee and not is_on_probation:
        # Get approved leave requests for current year
        from form_app.models import LeaveRequest
        current_year_requests = LeaveRequest.objects.filter(
            employee=employee,
            status='APPROVED',
            start_date__year=date.today().year,
            is_paid=True
        )
        
        # Calculate used leaves by type
        vacation_used = sum(
            req.total_days() for req in current_year_requests 
            if req.leave_type == 'VACATION'
        )
        sick_used = sum(
            req.total_days() for req in current_year_requests 
            if req.leave_type == 'SICK'
        )
        
        leave_balance = {
            "vacation": {"total": 14, "used": vacation_used, "remaining": 14 - vacation_used},
            "sick": {"total": 12, "used": sick_used, "remaining": 12 - sick_used}
        }
    else:
        leave_balance = None
    
    # Get pending requests count
    from form_app.models import LeaveRequest
    pending_requests = LeaveRequest.objects.filter(
        employee=employee,
        status='PENDING'
    ).count() if employee else 0

    return Response({
        "message": f"Hello {name}!",
        "name": name,
        "role": profile.role,
        "is_on_probation": is_on_probation,
        "probation_end_date": employee.probation_end_date if employee and is_on_probation else None,
        "leave_balance": leave_balance,
        "pending_requests": pending_requests
    })