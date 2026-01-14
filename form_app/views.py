
from datetime import date
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .serializers import LeaveCreateSerializer, LeaveResponseSerializer
from .models import LeaveRequest
from user_app.models import Profile

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_leave(request):

    # Block admins from applying for leave
    if hasattr(request.user, 'profile') and request.user.profile.role == 'ADMIN':
        return Response(
            {'error': 'Admins cannot apply for leave'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    serializer = LeaveCreateSerializer(
        data=request.data,
        context={'request': request}
    )

    if serializer.is_valid():
        leave = serializer.save()
        
        try:
            from discord_app.services import send_leave_request_to_admin
            send_leave_request_to_admin(leave)
        except ImportError:
            pass
        
        return Response(
            {
                "message": "Leave request submitted successfully",
                "data": LeaveResponseSerializer(leave).data
            },
            status=status.HTTP_201_CREATED
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_requests(request):

    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response(
                {"error": "Employee record not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        requests_list = []
        leave_requests = LeaveRequest.objects.filter(
            employee=employee
        ).order_by('-applied_at')

        for req in leave_requests:
            # Format date range
            if req.start_date == req.end_date:
                date_range = req.start_date.strftime("%b %d, %Y")
            else:
                date_range = f"{req.start_date.strftime('%b %d, %Y')} - {req.end_date.strftime('%b %d, %Y')}"
            
            requests_list.append({
                "id": req.id,
                "type": req.get_leave_type_display(),
                "status": req.status.capitalize(),
                "dateRange": date_range,
                "days": req.total_days(),
                "reason": req.reason or "-",
                "requested": req.applied_at.strftime("%b %d, %Y"),
                "reviewed": "-",  
                "reviewer": "-" 
            })

        return Response(requests_list)

    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )