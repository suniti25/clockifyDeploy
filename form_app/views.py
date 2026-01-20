from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
import logging

from .serializers import LeaveCreateSerializer, LeaveResponseSerializer
from .models import LeaveRequest
from user_app.models import Profile

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_leave(request):
    """
    Endpoint to apply for a new leave request.
    """
    # Block admins from applying for leave
    if hasattr(request.user, 'profile') and request.user.profile.role == 'ADMIN':
        return Response(
            {'error': 'Admins cannot apply for leave'},
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = LeaveCreateSerializer(data=request.data, context={'request': request})

    if serializer.is_valid():
        leave = serializer.save()

        # Send Discord notification 
        try:
            from discord_app.services import send_leave_request_to_admin
            send_leave_request_to_admin(leave)
        except ImportError:
            pass

        response_data = LeaveResponseSerializer(leave).data

        # Include warning if present
        if hasattr(serializer.context, 'warning'):
            response_data['warning'] = serializer.context['warning']

        return Response(
            {
                "message": "Leave request submitted successfully",
                "data": response_data
            },
            status=status.HTTP_201_CREATED
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# Update Leave
@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def update_leave(request, pk):

    try:
        leave = LeaveRequest.objects.get(
            id=pk,
            employee=request.user.profile.employee
        )
    except LeaveRequest.DoesNotExist:
        return Response(
            {"error": "Leave request not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    if leave.status != 'PENDING':
        return Response(
            {"error": "Only pending leave requests can be updated."},
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = LeaveCreateSerializer(
        instance=leave,
        data=request.data,
        context={'request': request},
        partial=True
    )

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    updated_leave = serializer.save()

    #  UPDATE DISCORD MESSAGE 
    if updated_leave.discord_message_id:
        logger.info(f"Updating Discord message {updated_leave.discord_message_id} for leave {updated_leave.id}")
        try:
            from discord_app.services import update_admin_leave_message
            result = update_admin_leave_message(updated_leave)
            logger.info(f"Discord update result: {result}")
        except Exception as e:
            logger.error(f"Discord update failed: {str(e)}", exc_info=True)
    else:
        logger.warning(f"No discord_message_id found for leave {updated_leave.id}")

    return Response(
        {
            "message": "Leave request updated successfully",
            "data": LeaveResponseSerializer(updated_leave).data
        },
        status=status.HTTP_200_OK
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_requests(request):
    """
    Endpoint to list all leave requests of the logged-in user.
    """
    try:
        profile = Profile.objects.select_related('employee').get(user=request.user)
        employee = profile.employee

        if not employee:
            return Response(
                {"error": "Employee record not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        requests_list = []
        leave_requests = LeaveRequest.objects.filter(employee=employee).order_by('-applied_at')

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
            })

        return Response(requests_list)

    except Profile.DoesNotExist:
        return Response(
            {"error": "Profile not found"},
            status=status.HTTP_404_NOT_FOUND
        )
