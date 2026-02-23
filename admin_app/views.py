from __future__ import annotations

import logging
from math import ceil

from django.contrib.auth.models import User
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Prefetch
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from form_app.models import LeaveRequest
from form_app.policies import get_leave_policy_settings, get_next_renewal_date
from form_app.services import decide_leave
from user_app.models import Employee, Profile

from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from .helpers import _norm_status_expr, serialize_request_for_frontend
from .permissions import IsAdminRole
from .serializers import (
    AdminEmployeeUpdateSerializer,
    AdminUserCreateSerializer,
    AdminUserUpdateSerializer,
    AllUsersDetailSerializer,
    EmployeeDetailSerializer,
    EmployeeRenewalScheduleSerializer,
    LeavePolicySettingsSerializer,
    LeaveRenewalOverrideSerializer,
    ProjectCreateSerializer,
    ProjectSerializer,
)
from .services import (
    AdminRequestServices,
    leave_trends_for_year,
    parse_kpi_month_year_params,
    sick_leave_kpi_for_current_month,
    vacation_leave_kpi_for_current_month,
    wfh_leave_kpi_for_current_month,
)

logger = logging.getLogger(__name__)


class AdminProjectsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List projects (optional ?active=true to return only active).",
        parameters=[
            OpenApiParameter(
                name="active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
            )
        ],
        responses={200: ProjectSerializer(many=True)},
    )
    def get(self, request):
        from user_app.models import Project

        qs = Project.objects.order_by("name")
        only_active = request.GET.get("active")
        if str(only_active).lower() in ("1", "true", "yes"):
            qs = qs.filter(is_active=True)

        return Response(ProjectSerializer(qs, many=True).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Create a project.",
        request=ProjectCreateSerializer,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request):
        ser = ProjectCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        proj = ser.save()
        return Response(
            {
                "status": "success",
                "message": "Project created successfully",
                "data": ProjectSerializer(proj).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminProjectDeleteByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Soft delete a project by body (project_id).",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        project_id = request.data.get("project_id")
        if not project_id:
            return Response({"error": "project_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        from user_app.models import Project

        try:
            proj = Project.objects.select_for_update().get(id=int(project_id))
        except (ValueError, TypeError):
            return Response({"error": "Invalid project_id"}, status=status.HTTP_400_BAD_REQUEST)
        except Project.DoesNotExist:
            return Response({"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND)

        proj.is_active = False
        proj.save(update_fields=["is_active"])

        Employee.objects.filter(current_project=proj.name).update(current_project="")

        return Response(
            {"status": "success", "message": "Project deleted successfully"},
            status=status.HTTP_200_OK,
        )

    @extend_schema(exclude=True)
    def delete(self, request):
        return self.post(request)


class AdminLeaveKPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Get combined leave KPIs (supports month/year params used by the service).",
        parameters=[
            OpenApiParameter(name="month", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="year", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request):
        try:
            year, month = parse_kpi_month_year_params(request.GET)
            return Response(
                {
                    "status": "success",
                    "data": {
                        "sick_leave": sick_leave_kpi_for_current_month(year=year, month=month),
                        "vacation_leave": vacation_leave_kpi_for_current_month(year=year, month=month),
                        "wfh": wfh_leave_kpi_for_current_month(year=year, month=month),
                        "leave_trends": leave_trends_for_year(year=year),
                    },
                },
                status=status.HTTP_200_OK,
            )
        except (TypeError, ValueError) as e:
            return Response({"status": "error", "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("Admin leave KPI combined failed")
            return Response(
                {"status": "error", "message": "Failed to calculate leave KPIs", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AllUsersDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List all users with profile/employee and prefetched leave requests.",
        responses={200: AllUsersDetailSerializer(many=True)},
    )
    def get(self, request):
        leaves_qs = LeaveRequest.objects.order_by("-applied_at", "-id")
        users = (
            User.objects.select_related("profile", "employee")
            .prefetch_related(
                Prefetch("employee__leave_requests", queryset=leaves_qs, to_attr="prefetched_leaves")
            )
        )
        return Response(AllUsersDetailSerializer(users, many=True).data, status=status.HTTP_200_OK)


class UserDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Get a single user (admin) by user_id.",
        responses={200: AllUsersDetailSerializer, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, user_id: int):
        try:
            user = User.objects.select_related("profile", "employee").get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        if getattr(user, "employee", None):
            user.employee.prefetched_leaves = list(
                LeaveRequest.objects.filter(employee=user.employee).order_by("-applied_at", "-id")
            )

        return Response(AllUsersDetailSerializer(user).data, status=status.HTTP_200_OK)


class AllEmployeesDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List all employees (excluding staff/superuser).",
        responses={200: EmployeeDetailSerializer(many=True)},
    )
    def get(self, request):
        employees = (
            Employee.objects.select_related("user")
            .exclude(user__is_staff=True)
            .exclude(user__is_superuser=True)
        )
        return Response(EmployeeDetailSerializer(employees, many=True).data, status=status.HTTP_200_OK)


class EmployeesByRoleView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List employee users (profile role=EMPLOYEE) with prefetched leave requests.",
        responses={200: AllUsersDetailSerializer(many=True)},
    )
    def get(self, request):
        leaves_qs = LeaveRequest.objects.order_by("-applied_at", "-id")
        users = (
            User.objects.filter(profile__role="EMPLOYEE")
            .select_related("profile", "employee")
            .prefetch_related(
                Prefetch("employee__leave_requests", queryset=leaves_qs, to_attr="prefetched_leaves")
            )
        )
        return Response(AllUsersDetailSerializer(users, many=True).data, status=status.HTTP_200_OK)


class AdminEmployeeDetailUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Get employee detail by employee_id.",
        responses={200: EmployeeDetailSerializer, 404: OpenApiTypes.OBJECT},
    )
    def get(self, request, employee_id: int):
        try:
            emp = Employee.objects.select_related("user").get(id=employee_id)
        except Employee.DoesNotExist:
            return Response({"error": "Employee not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(EmployeeDetailSerializer(emp).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Update employee by employee_id (PATCH).",
        request=AdminEmployeeUpdateSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def patch(self, request, employee_id: int):
        payload = dict(request.data or {})
        payload["employee_id"] = employee_id

        ser = AdminEmployeeUpdateSerializer(data=payload)
        ser.is_valid(raise_exception=True)
        emp = ser.update_employee()

        return Response(
            {
                "status": "success",
                "message": "Employee updated successfully",
                "data": EmployeeDetailSerializer(emp).data,
            },
            status=status.HTTP_200_OK,
        )


class AdminEmployeeUpdateByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Update employee by body (PATCH).",
        request=AdminEmployeeUpdateSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def patch(self, request):
        ser = AdminEmployeeUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        emp = ser.update_employee()

        return Response(
            {
                "status": "success",
                "message": "Employee updated successfully",
                "data": EmployeeDetailSerializer(emp).data,
            },
            status=status.HTTP_200_OK,
        )


class AdminDashboardStatsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Admin dashboard stats (total employees, users on probation).",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        today = timezone.localdate()
        return Response(
            {
                "total_employees": Employee.objects.count(),
                "users_on_probation": Employee.objects.filter(
                    joining_date__lte=today, probation_end_date__gte=today
                ).count(),
            },
            status=status.HTTP_200_OK,
        )


class AdminAllRequestsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List all leave requests for admin with paging and filters.",
        parameters=[
            OpenApiParameter(name="page", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="page_size", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="pageSize", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="per_page", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="limit", type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.GET

        def _first_present_int(keys: tuple[str, ...], default: int) -> int:
            for key in keys:
                raw = params.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return default
            return default

        page = _first_present_int(("page", "current_page", "currentPage", "pageNumber"), 1)
        page_size = _first_present_int(("page_size", "pageSize", "per_page", "perPage", "limit"), 10)

        page = max(page, 1)
        page_size = min(max(page_size, 1), 50)

        counts_qs = AdminRequestServices.unfiltered_queryset()
        counts = AdminRequestServices.get_status_counts(counts_qs)

        ordering = AdminRequestServices.ordering_for_params(params)
        list_qs = AdminRequestServices.base_queryset(params).order_by(*ordering)
        total = list_qs.count()
        total_pages = ceil(total / page_size) if total else 0

        start = (page - 1) * page_size
        end = start + page_size
        page_qs = list_qs[start:end]
        requests_data = [serialize_request_for_frontend(lr) for lr in page_qs]

        return Response(
            {
                "counts": counts,
                "count": total,
                "filtered_count": total,
                "page_size": page_size,
                "total_pages": total_pages,
                "current_page": page,
                "next": page + 1 if end < total else None,
                "previous": page - 1 if page > 1 else None,
                "requests": requests_data,
            },
            status=status.HTTP_200_OK,
        )


class AdminPendingRequestsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Admin pending requests summary + recent requests + top leave takers.",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.GET
        base_qs = AdminRequestServices.unfiltered_queryset().order_by("-applied_at", "-id")

        recent_qs = base_qs[:7]
        recent_requests = [serialize_request_for_frontend(lr) for lr in recent_qs]

        pending_qs = base_qs.annotate(_s=_norm_status_expr("status")).filter(_s=LeaveRequest.STATUS_PENDING)

        pending_total = pending_qs.count()
        pending_requests = [serialize_request_for_frontend(lr) for lr in pending_qs]

        today = timezone.localdate()
        summary = {
            "team_members": Profile.objects.filter(role="EMPLOYEE", user__employee__isnull=False).count(),
            "pending_requests": (
                LeaveRequest.objects.annotate(_s=_norm_status_expr("status"))
                .filter(_s=LeaveRequest.STATUS_PENDING)
                .count()
            ),
            "approved_requests_applied_today": (
                LeaveRequest.objects.annotate(_s=_norm_status_expr("status"))
                .filter(_s=LeaveRequest.STATUS_APPROVED, applied_at__date=today)
                .count()
            ),
        }

        top_leave_takers = AdminRequestServices.top_leave_takers(params)

        return Response(
            {
                "summary": summary,
                "pending": pending_total,
                "count": pending_total,
                "top_leave_takers": top_leave_takers,
                "pending_requests": pending_requests,
                "recents": recent_requests,
            },
            status=status.HTTP_200_OK,
        )


class AdminUserCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Create a user (admin).",
        request=AdminUserCreateSerializer,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request):
        ser = AdminUserCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()

        return Response(
            {
                "status": "success",
                "message": "User created successfully",
                "data": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminUserUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Update a user (admin) by body (PATCH).",
        request=AdminUserUpdateSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def patch(self, request):
        ser = AdminUserUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        update_user_fn = getattr(ser, "update_user", None)
        if callable(update_user_fn):
            user = ser.update_user()
        else:
            user_id = ser.validated_data["user_id"]
            try:
                user = User.objects.select_related("employee").get(id=user_id)
            except User.DoesNotExist:
                return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

            data = ser.validated_data
            for field in ("username", "first_name", "last_name", "email"):
                if field in data:
                    setattr(user, field, data[field])

            pwd = (data.get("password") or "").strip()
            if pwd:
                user.set_password(pwd)

            user.save()

            emp = getattr(user, "employee", None)
            if emp:
                if "current_project" in data and data["current_project"] is not None:
                    emp.current_project = data["current_project"] or ""
                emp.name = user.get_full_name().strip() or user.username
                emp.save()

        return Response(
            {
                "status": "success",
                "message": "User updated successfully",
                "data": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "on_probation": (
                        user.employee.is_on_probation()
                        if getattr(user, "employee", None) and callable(getattr(user.employee, "is_on_probation", None))
                        else None
                    ),
                },
            },
            status=status.HTTP_200_OK,
        )


class AdminUserDeleteByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Delete a user by body (user_id).",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        try:
            user_id = request.data.get("user_id")
            if not user_id:
                return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

            try:
                user = User.objects.select_for_update().get(id=int(user_id))
            except (User.DoesNotExist, ValueError, TypeError):
                return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

            if request.user and user.id == request.user.id:
                return Response(
                    {"error": "You cannot delete your own account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if user.is_staff or user.is_superuser:
                return Response(
                    {"error": "Admin accounts cannot be deleted."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                target_profile = user.profile
            except ObjectDoesNotExist:
                target_profile = None

            if getattr(target_profile, "role", None) == Profile.ROLE_ADMIN:
                return Response(
                    {"error": "Admin accounts cannot be deleted."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                user.delete()
            except ProtectedError:
                return Response(
                    {
                        "status": "error",
                        "message": "User cannot be deleted because related records exist.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            return Response(
                {"status": "success", "message": "User deleted successfully"},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Admin user delete failed")
            payload = {"status": "error", "message": "Failed to delete user"}
            if getattr(settings, "DEBUG", False):
                payload["detail"] = str(e)
                payload["exception"] = e.__class__.__name__
            return Response(payload, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(exclude=True)
    def delete(self, request):
        return self.post(request)


class LeavePolicySettingsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Get leave policy settings.",
        responses={200: LeavePolicySettingsSerializer},
    )
    def get(self, request):
        settings_obj = get_leave_policy_settings()
        return Response(LeavePolicySettingsSerializer(settings_obj).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Update leave policy settings (partial).",
        request=LeavePolicySettingsSerializer,
        responses={200: LeavePolicySettingsSerializer, 400: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        settings_obj = get_leave_policy_settings()
        ser = LeavePolicySettingsSerializer(settings_obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_200_OK)


class EmployeeRenewalScheduleView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List employees with next renewal date (computed).",
        responses={200: EmployeeRenewalScheduleSerializer(many=True)},
    )
    def get(self, request):
        employees = (
            Employee.objects.select_related("user")
            .exclude(user__is_staff=True)
            .exclude(user__is_superuser=True)
        )

        for emp in employees:
            emp.next_renewal_date = get_next_renewal_date(emp)

        return Response(EmployeeRenewalScheduleSerializer(employees, many=True).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Override an employee renewal date.",
        request=LeaveRenewalOverrideSerializer,
        responses={200: EmployeeRenewalScheduleSerializer, 400: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        ser = LeaveRenewalOverrideSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        employee_id = ser.validated_data["employee_id"]
        override = ser.validated_data.get("leave_renewal_date_override", None)

        emp = Employee.objects.select_related("user").get(id=employee_id)
        emp.leave_renewal_date_override = override
        emp.save(update_fields=["leave_renewal_date_override"])

        emp.next_renewal_date = get_next_renewal_date(emp)
        return Response(EmployeeRenewalScheduleSerializer(emp).data, status=status.HTTP_200_OK)


class ApproveLeaveByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Approve leave by formID (or form_id) in request body.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request):
        form_id = request.data.get("formID") or request.data.get("form_id")
        message = (request.data.get("message") or "").strip() or None

        if not form_id:
            return Response({"error": "formID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lr = decide_leave(
                leave_id=int(form_id),
                new_status=LeaveRequest.STATUS_APPROVED,
                message=message,
            )
        except (ValueError, TypeError):
            return Response({"error": "Invalid formID"}, status=status.HTTP_400_BAD_REQUEST)
        except LeaveRequest.DoesNotExist:
            return Response({"error": "Leave request not found"}, status=status.HTTP_404_NOT_FOUND)

        lr.refresh_from_db()
        return Response(
            {
                "detail": "Approved",
                "formID": lr.id,
                "status": (lr.status or "").strip().upper(),
                "message": message,
            },
            status=status.HTTP_200_OK,
        )


class RejectLeaveByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Reject leave by formID (or form_id) in request body.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def post(self, request):
        form_id = request.data.get("formID") or request.data.get("form_id")
        message = (request.data.get("message") or "").strip()

        if not form_id:
            return Response({"error": "formID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lr = decide_leave(
                leave_id=int(form_id),
                new_status=LeaveRequest.STATUS_REJECTED,
                message=message,
            )
        except (ValueError, TypeError):
            return Response({"error": "Invalid formID"}, status=status.HTTP_400_BAD_REQUEST)
        except LeaveRequest.DoesNotExist:
            return Response({"error": "Leave request not found"}, status=status.HTTP_404_NOT_FOUND)

        lr.refresh_from_db()
        return Response(
            {
                "detail": "Rejected",
                "formID": lr.id,
                "status": (lr.status or "").strip().upper(),
                "message": message,
            },
            status=status.HTTP_200_OK,
        )