from __future__ import annotations

import logging

from django.contrib.auth import authenticate
from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from .models import Employee, LeaveRequest, Profile
from .serializers import EmployeeSerializer, LeaveRequestSerializer, LoginSerializer

logger = logging.getLogger(__name__)


# Authentication View
@extend_schema(
    description="Login using username/password and return JWT access/refresh tokens.",
    request=LoginSerializer,
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        401: OpenApiTypes.OBJECT,
    },
)
@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    ser = LoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    user = authenticate(
        username=ser.validated_data["username"],
        password=ser.validated_data["password"],
    )
    if not user:
        return Response(
            {"error": ["Invalid credentials"]}, status=status.HTTP_401_UNAUTHORIZED
        )

    try:
        profile = Profile.objects.get(user=user)
    except Profile.DoesNotExist:
        return Response(
            {"error": ["User profile not found"]}, status=status.HTTP_400_BAD_REQUEST
        )

    refresh = RefreshToken.for_user(user)

    return Response(
        {
            "message": "Login successful",
            "user": {"username": user.username, "role": profile.role},
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        },
        status=status.HTTP_200_OK,
    )


# ViewSets
class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]


class LeaveRequestViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        try:
            profile = self.request.user.profile
        except Profile.DoesNotExist:
            return LeaveRequest.objects.none()

        if profile.role == "ADMIN":
            return LeaveRequest.objects.all()

        return LeaveRequest.objects.filter(employee=profile.employee)

    def perform_create(self, serializer):
        serializer.save(employee=self.request.user.profile.employee)

    @extend_schema(
        description="Admin-only: update a leave request status.",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
        },
    )
    @action(
        detail=True,
        methods=["patch"],
        permission_classes=[IsAuthenticated, IsAdminUser],
    )
    def update_status(self, request, pk=None):
        leave = self.get_object()
        status_value = (request.data.get("status") or "").strip().upper()

        if status_value not in ("APPROVED", "REJECTED"):
            return Response(
                {"error": ["Invalid status"]}, status=status.HTTP_400_BAD_REQUEST
            )

        leave.status = status_value
        leave.save(update_fields=["status"])

        return Response(
            {
                "message": f"Leave {status_value.lower()} successfully",
                "leave_id": leave.id,
                "status": leave.status,
            },
            status=status.HTTP_200_OK,
        )


# Functions for custom endpoints
@extend_schema(
    description="Get all leave requests (typically used for admin dashboard).",
    responses={200: LeaveRequestSerializer(many=True)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def leave_list(request):
    leaves = LeaveRequest.objects.all()
    return Response(
        LeaveRequestSerializer(leaves, many=True).data, status=status.HTTP_200_OK
    )


@extend_schema(
    description="Get leave requests belonging to the logged-in employee.",
    responses={
        200: LeaveRequestSerializer(many=True),
        400: OpenApiTypes.OBJECT,
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_leaves(request):
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return Response(
            {"error": ["User profile not found"]}, status=status.HTTP_400_BAD_REQUEST
        )

    leaves = LeaveRequest.objects.filter(employee=profile.employee)
    return Response(
        LeaveRequestSerializer(leaves, many=True).data, status=status.HTTP_200_OK
    )


@extend_schema(
    description="Admin-only: approve a leave request by id.",
    parameters=[
        OpenApiParameter(
            name="pk",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.PATH,
            required=True,
        ),
    ],
    responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated, IsAdminUser])
def approve_leave(request, pk: int):
    leave = get_object_or_404(LeaveRequest, pk=pk)
    leave.status = "APPROVED"
    leave.save(update_fields=["status"])
    return Response({"message": "Leave Approved"}, status=status.HTTP_200_OK)


@extend_schema(
    description="Admin-only: reject a leave request by id.",
    parameters=[
        OpenApiParameter(
            name="pk",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.PATH,
            required=True,
        ),
    ],
    responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
)
@api_view(["PATCH"])
@permission_classes([IsAuthenticated, IsAdminUser])
def reject_leave(request, pk: int):
    leave = get_object_or_404(LeaveRequest, pk=pk)
    leave.status = "REJECTED"
    leave.save(update_fields=["status"])
    return Response({"message": "Leave Rejected"}, status=status.HTTP_200_OK)
