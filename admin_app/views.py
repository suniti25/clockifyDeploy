from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from user_app.models import Employee, Profile
from form_app.models import LeaveRequest 

from .serializers import AllUsersDetailSerializer, EmployeeDetailSerializer


def _require_admin(request):
    """
    Returns (profile, error_response). If user is admin -> (profile, None)
    """
    profile = getattr(request.user, "profile", None)
    if not profile:
        return None, Response(
            {"error": "User profile not found"},
            status=status.HTTP_403_FORBIDDEN
        )

    if profile.role != "ADMIN":
        return profile, Response(
            {"error": "Only admins can access this endpoint"},
            status=status.HTTP_403_FORBIDDEN
        )

    return profile, None


class AllUsersDetailView(APIView):
    """
    Get all users with profile/employee info. Admin only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, err = _require_admin(request)
        if err:
            return err

        users = User.objects.select_related("profile", "employee").all()
        serializer = AllUsersDetailSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AllEmployeesDetailView(APIView):
    """
    Get all employees with their details. Admin only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, err = _require_admin(request)
        if err:
            return err

        employees = (
            Employee.objects.select_related("user")
            .exclude(user__is_staff=True)
            .exclude(user__is_superuser=True)
        )
        serializer = EmployeeDetailSerializer(employees, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmployeesByRoleView(APIView):
    """
    List all users whose profile role is EMPLOYEE.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, err = _require_admin(request)
        if err:
            return err

        users = User.objects.filter(profile__role="EMPLOYEE").select_related("profile", "employee")
        serializer = AllUsersDetailSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class UserDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        _, err = _require_admin(request)
        if err:
            return err

        try:
            user = User.objects.select_related("profile", "employee").get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = AllUsersDetailSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AdminDashboardSummaryView(APIView):
    """
     Cards for Admin Dashboard UI:
    - team_members: total employees who have EMPLOYEE role
    - pending_requests: total pending leave requests
    - approved_today: total approved today
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, err = _require_admin(request)
        if err:
            return err

        today = timezone.localdate()

        # Team members = user profiles that are EMPLOYEE AND have Employee account
        team_members = Profile.objects.filter(role="EMPLOYEE", user__employee__isnull=False).count()

        pending_requests = LeaveRequest.objects.filter(status="PENDING").count()

        approved_today = LeaveRequest.objects.filter(status="APPROVED", applied_at__date=today).count()

        return Response(
            {
                "team_members": team_members,
                "pending_requests": pending_requests,
                "approved_today": approved_today,
            },
            status=status.HTTP_200_OK,
        )


class AdminDashboardStatsView(APIView):
    """
    Admin dashboard statistics. Admin only.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _, err = _require_admin(request)
        if err:
            return err

        stats = {
            "total_employees": Employee.objects.count(),
            "users_on_probation": Employee.objects.filter(
                probation_end_date__gt=timezone.localdate()
            ).count(),
        }

        return Response(stats, status=status.HTTP_200_OK)
