from django.urls import path

from auth_app.views import RefreshTokenView
from .views import (AllUsersDetailView,AllEmployeesDetailView,UserDetailView,AdminDashboardStatsView,EmployeesByRoleView,  UserDetailView, AdminDashboardStatsView, AdminDashboardSummaryView,)

urlpatterns = [
    # Get all users with details
    path('users/all/', AllUsersDetailView.as_view(), name='all-users-detail'),
    
    # Get all employees with details
    path('employees/all/', AllEmployeesDetailView.as_view(), name='all-employees-detail'),
    
    # Get all users whose role=EMPLOYEE (even without Employee row)
    path('employees/by-role/', EmployeesByRoleView.as_view(), name='employees-by-role'),
    
    # Get specific user by ID
    path('users/<int:user_id>/', UserDetailView.as_view(), name='user-detail'),
    
    # Get admin dashboard statistics
    path('dashboard/stats/', AdminDashboardStatsView.as_view(), name='dashboard-stats'),

    # path('admin/', admin.site.urls),
    path("cookie/", RefreshTokenView.as_view()),

    #path ('admin/dashboard/stats/', AdminDashboardStatsView.as_view()),

    path("dashboard/summary/", AdminDashboardSummaryView.as_view()),  
]