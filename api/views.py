from django.contrib.auth import authenticate
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework_simplejwt.tokens import RefreshToken
from .models import Employee, LeaveRequest, Profile
from .serializers import EmployeeSerializer, LeaveRequestSerializer, LoginSerializer

# Authentication View 

@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    user = authenticate(
        username=serializer.validated_data['username'],
        password=serializer.validated_data['password']
    )

    if not user:
        return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        profile = Profile.objects.get(user=user)
    except Profile.DoesNotExist:
        return Response({"error": "User profile not found"}, status=status.HTTP_400_BAD_REQUEST)

    refresh = RefreshToken.for_user(user)

    return Response({
        "message": "Login successful",
        "user": {
            "username": user.username,
            "role": profile.role,
        },
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    })

# ViewSets 

class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]


class LeaveRequestViewSet(viewsets.ModelViewSet):
    """
    - Admins can see and manage all leave requests
    - Employees can only see and create their own
    """
    serializer_class = LeaveRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        try:
            profile = self.request.user.profile
        except Profile.DoesNotExist:
            return LeaveRequest.objects.none()

        if profile.role == 'ADMIN':
            return LeaveRequest.objects.all()

        return LeaveRequest.objects.filter(employee=profile.employee)

    def perform_create(self, serializer):
        serializer.save(employee=self.request.user.profile.employee)

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated, IsAdminUser])
    def update_status(self, request, pk=None):
        leave = self.get_object()
        status_value = request.data.get("status")

        if status_value not in ["APPROVED", "REJECTED"]:
            return Response({"error": "Invalid status"}, status=status.HTTP_400_BAD_REQUEST)

        leave.status = status_value
        leave.save()

        return Response({
            "message": f"Leave {status_value.lower()} successfully",
            "leave_id": leave.id,
            "status": leave.status
        })

# Funtions for custom endpoints

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def leave_list(request):
    """Get all leaves (Usually for Admin dashboard)"""
    leaves = LeaveRequest.objects.all()
    serializer = LeaveRequestSerializer(leaves, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_leaves(request):
    """Get leaves belonging to the logged-in employee"""
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return Response({"error": "User profile not found"}, status=status.HTTP_400_BAD_REQUEST)
    
    leaves = LeaveRequest.objects.filter(employee=profile.employee)
    serializer = LeaveRequestSerializer(leaves, many=True)
    return Response(serializer.data)

@api_view(['PATCH'])
@permission_classes([IsAuthenticated, IsAdminUser]) 
def approve_leave(request, pk):
    leave = get_object_or_404(LeaveRequest, pk=pk)
    leave.status = "APPROVED"
    leave.save()
    return Response({"message": "Leave Approved"})

@api_view(['PATCH'])
@permission_classes([IsAuthenticated, IsAdminUser]) 
def reject_leave(request, pk):
    leave = get_object_or_404(LeaveRequest, pk=pk)
    leave.status = "REJECTED"
    leave.save()
    return Response({"message": "Leave Rejected"})