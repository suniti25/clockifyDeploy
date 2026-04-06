# admin_app/urls.py
from django.urls import include, path

from .views import (
    AdminAllRequestsView,
    AdminDashboardStatsView,
    AdminEmployeeDetailUpdateView,
    AdminEmployeeUpdateByBodyView,
    AdminHolidayDeleteByBodyView,
    AdminHolidaysSettingsView,
    AdminLeaveDeleteByBodyView,
    AdminRefreshDailyLeaveMessageView,
    AdminLeaveKPIView,
    AdminPendingRequestsView,
    AdminTopLeaveTakersView,
    AdminProjectsView,
    AdminUserCreateView,
    AdminUserUpdateView,
    AllEmployeesDetailView,
    AllUsersDetailView,
    ApproveLeaveByBodyView,
    EmployeesByRoleView,
    EmployeeRenewalScheduleView,
    LeavePolicySettingsView,
    RejectLeaveByBodyView,
    UserDetailView,
    AdminProjectDeleteByBodyView,
    AdminUserDeleteByBodyView,
)

urlpatterns = [
    # Users / Employees
    path("users/all/", AllUsersDetailView.as_view(), name="admin-users-all"),
    path("users/<int:user_id>/", UserDetailView.as_view(), name="admin-user-detail"),
    path(
        "users/delete/", AdminUserDeleteByBodyView.as_view(), name="admin-user-delete"
    ),
    path(
        "employees/all/", AllEmployeesDetailView.as_view(), name="admin-employees-all"
    ),
    path(
        "employees/by-role/",
        EmployeesByRoleView.as_view(),
        name="admin-employees-by-role",
    ),
    path(
        "employees/<int:employee_id>/",
        AdminEmployeeDetailUpdateView.as_view(),
        name="admin-employee-detail-update",
    ),
    # Dashboard
    path(
        "dashboard/stats/",
        AdminDashboardStatsView.as_view(),
        name="admin-dashboard-stats",
    ),
    # Requests
    path("requests/all/", AdminAllRequestsView.as_view(), name="admin-requests-all"),
    path(
        "requests/pending/",
        AdminPendingRequestsView.as_view(),
        name="admin-requests-pending",
    ),
    path(
        "requests/top-leave-takers/",
        AdminTopLeaveTakersView.as_view(),
        name="admin-top-leave-takers",
    ),
    # KPI
    path("kpis/leave/", AdminLeaveKPIView.as_view(), name="admin-leave-kpi"),
    # Leave decisions
    path(
        "leaves/approve/", ApproveLeaveByBodyView.as_view(), name="admin-approve-leave"
    ),
    path("leaves/reject/", RejectLeaveByBodyView.as_view(), name="admin-reject-leave"),
    path(
        "leaves/delete/",
        AdminLeaveDeleteByBodyView.as_view(),
        name="admin-delete-leave",
    ),
    path(
        "leaves/refresh-daily-message/",
        AdminRefreshDailyLeaveMessageView.as_view(),
        name="admin-refresh-daily-message",
    ),
    # Admin Settings
    path(
        "settings/",
        include(
            [
                path(
                    "users/create/",
                    AdminUserCreateView.as_view(),
                    name="admin-user-create",
                ),
                path(
                    "users/update/",
                    AdminUserUpdateView.as_view(),
                    name="admin-user-update",
                ),
                path(
                    "employees/update/",
                    AdminEmployeeUpdateByBodyView.as_view(),
                    name="admin-employee-update",
                ),
                path("projects/", AdminProjectsView.as_view(), name="admin-projects"),
                path(
                    "projects/delete/",
                    AdminProjectDeleteByBodyView.as_view(),
                    name="admin-project-delete",
                ),
                path(
                    "leave/",
                    LeavePolicySettingsView.as_view(),
                    name="admin-leave-settings",
                ),
                path(
                    "holidays/",
                    AdminHolidaysSettingsView.as_view(),
                    name="admin-holidays-settings",
                ),
                path(
                    "holidays/delete/",
                    AdminHolidayDeleteByBodyView.as_view(),
                    name="admin-holiday-delete",
                ),
                path(
                    "renewals/",
                    EmployeeRenewalScheduleView.as_view(),
                    name="admin-renewal-schedule",
                ),
            ]
        ),
    ),
]
