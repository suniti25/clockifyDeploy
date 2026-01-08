from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.models import User
from user_app.models import Employee, Profile
from .serializers import (
    AllUsersDetailSerializer,
    EmployeeDetailSerializer,
)


class AllUsersDetailView(APIView):
    """
    API endpoint to get all users with their details including profile and employee information.
    Only accessible to admin users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Check if user is admin
        try:
            user_profile = request.user.profile
            if user_profile.role != 'ADMIN':
                return Response(
                    {'error': 'Only admins can access this endpoint'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Profile.DoesNotExist:
            return Response(
                {'error': 'User profile not found'},
                status=status.HTTP_403_FORBIDDEN
            )

        users = User.objects.all().prefetch_related('profile', 'employee')
        serializer = AllUsersDetailSerializer(users, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AllEmployeesDetailView(APIView):
    """
    API endpoint to get all employees with their details.
    Only accessible to admin users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Check if user is admin
        try:
            user_profile = request.user.profile
            if user_profile.role != 'ADMIN':
                return Response(
                    {'error': 'Only admins can access this endpoint'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Profile.DoesNotExist:
            return Response(
                {'error': 'User profile not found'},
                status=status.HTTP_403_FORBIDDEN
            )

        employees = (
            Employee.objects.select_related('user')
            .exclude(user__is_staff=True)
            .exclude(user__is_superuser=True)
        )
        serializer = EmployeeDetailSerializer(employees, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmployeesByRoleView(APIView):
    """
    List all users whose profile role is EMPLOYEE, regardless of whether
    they have an Employee record. Useful when admin shows more EMPLOYEE users
    than there are Employee rows.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            user_profile = request.user.profile
            if user_profile.role != 'ADMIN':
                return Response(
                    {'error': 'Only admins can access this endpoint'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Profile.DoesNotExist:
            return Response(
                {'error': 'User profile not found'},
                status=status.HTTP_403_FORBIDDEN
            )

        users = User.objects.filter(profile__role='EMPLOYEE').prefetch_related('profile', 'employee')
        data = AllUsersDetailSerializer(users, many=True).data
        return Response(data, status=status.HTTP_200_OK)


class UserDetailView(APIView):
    """
    API endpoint to get a specific user's details by user ID.
    Only accessible to admin users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        # Check if user is admin
        try:
            user_profile = request.user.profile
            if user_profile.role != 'ADMIN':
                return Response(
                    {'error': 'Only admins can access this endpoint'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Profile.DoesNotExist:
            return Response(
                {'error': 'User profile not found'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            user = User.objects.prefetch_related('profile', 'employee').get(id=user_id)
            serializer = AllUsersDetailSerializer(user)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class AdminDashboardStatsView(APIView):
    """
    API endpoint to get admin dashboard statistics.
    Only accessible to admin users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Check if user is admin
        try:
            user_profile = request.user.profile
            if user_profile.role != 'ADMIN':
                return Response(
                    {'error': 'Only admins can access this endpoint'},
                    status=status.HTTP_403_FORBIDDEN
                )
        except Profile.DoesNotExist:
            return Response(
                {'error': 'User profile not found'},
                status=status.HTTP_403_FORBIDDEN
            )

        stats = {
            'total_users': User.objects.count(),
            'total_employees': Employee.objects.count(),
            'active_users': User.objects.filter(is_active=True).count(),
            'inactive_users': User.objects.filter(is_active=False).count(),
            'total_admins': Profile.objects.filter(role='ADMIN').count(),
            'total_employee_role': Profile.objects.filter(role='EMPLOYEE').count(),
            'users_on_probation': Employee.objects.filter(
                probation_end_date__gte=__import__('datetime').date.today()
            ).count(),
        }
        return Response(stats, status=status.HTTP_200_OK)
