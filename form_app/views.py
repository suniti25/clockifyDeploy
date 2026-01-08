from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .serializers import LeaveCreateSerializer, LeaveResponseSerializer


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_leave(request):
    serializer = LeaveCreateSerializer(
        data=request.data,
        context={'request': request}
    )

    if serializer.is_valid():
        leave = serializer.save()
        
        # Send notification to Discord admin channel (lazy import)
        try:
            from discord_app.services import send_leave_request_to_admin
            send_leave_request_to_admin(leave)
        except ImportError:
            print("Discord service not available")
        
        return Response(
            {
                "message": "Leave request submitted successfully",
                "data": LeaveResponseSerializer(leave).data
            },
            status=status.HTTP_201_CREATED
        )

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
