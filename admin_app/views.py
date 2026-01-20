from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from user_app.models import Employee, Profile
from .serializers import AllUsersDetailSerializer, EmployeeDetailSerializer


def _require_admin(request):
    """
    Returns (profile, error_response). If user is admin -> (profile, None)
    """
    try:
        profile = request.user.profile
    except Profile.DoesNotExist:
        return None, Response({"error": "User profile not found"}, status=status.HTTP_403_FORBIDDEN)

    if profile.role != "ADMIN":
        return profile, Response({"error": "Only admins can access this endpoint"}, status=status.HTTP_403_FORBIDDEN)

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

        # If Employee is OneToOne/ForeignKey from Employee -> User with related_name="employee"
        users = User.objects.all().select_related("profile")

        # select_related("employee") only works if relation name exists on User
        # If it doesn't, it's safe to remove it.
        try:
            users = users.select_related("employee")
        except Exception:
            pass

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

        users = User.objects.filter(profile__role="EMPLOYEE").select_related("profile")

        try:
            users = users.select_related("employee")
        except Exception:
            pass

        serializer = AllUsersDetailSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class UserDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        _, err = _require_admin(request)
        if err:
            return err

        try:
            user = User.objects.select_related("profile").get(id=user_id)
            try:
                user = User.objects.select_related("profile", "employee").get(id=user_id)
            except Exception:
                pass

            serializer = AllUsersDetailSerializer(user)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


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
            "total_users": User.objects.count(),
            "total_employees": Employee.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "inactive_users": User.objects.filter(is_active=False).count(),
            "total_admins": Profile.objects.filter(role="ADMIN").count(),
            "total_employee_role": Profile.objects.filter(role="EMPLOYEE").count(),
            "users_on_probation": Employee.objects.filter(
                probation_end_date__gt=timezone.localdate()
            ).count(),
        }

        return Response(stats, status=status.HTTP_200_OK)
