from __future__ import annotations

import logging
from math import ceil

from django.contrib.auth.models import User
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Prefetch, Q
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
    AdminHolidaySerializer,
    AdminHolidayBulkCreateSerializer,
    AdminHolidayUpsertSerializer,
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

        return Response(
            ProjectSerializer(qs, many=True).data, status=status.HTTP_200_OK
        )

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
            return Response(
                {"error": ["project_id is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from user_app.models import Project

        try:
            proj = Project.objects.select_for_update().get(id=int(project_id))
        except (ValueError, TypeError):
            return Response(
                {"error": ["Invalid project_id"]}, status=status.HTTP_400_BAD_REQUEST
            )
        except Project.DoesNotExist:
            return Response(
                {"error": ["Project not found"]}, status=status.HTTP_404_NOT_FOUND
            )

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
            OpenApiParameter(
                name="month",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="year",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
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
                        "sick_leave": sick_leave_kpi_for_current_month(
                            year=year, month=month
                        ),
                        "vacation_leave": vacation_leave_kpi_for_current_month(
                            year=year, month=month
                        ),
                        "wfh": wfh_leave_kpi_for_current_month(year=year, month=month),
                        "leave_trends": leave_trends_for_year(year=year),
                    },
                },
                status=status.HTTP_200_OK,
            )
        except (TypeError, ValueError) as e:
            return Response(
                {"status": "error", "message": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.exception("Admin leave KPI combined failed")
            return Response(
                {
                    "status": "error",
                    "message": "Failed to calculate leave KPIs",
                    "detail": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AllUsersDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List all users with profile/employee and prefetched leave requests.",
        parameters=[
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
            )
        ],
        responses={200: AllUsersDetailSerializer(many=True)},
    )
    def get(self, request):
        leaves_qs = LeaveRequest.objects.order_by(
            "-applied_at", "-id"
        ).prefetch_related("days")
        params = request.GET
        raw_search = (
            (params.get("search") or "")
            or (params.get("q") or "")
            or (params.get("name") or "")
        )
        search = (raw_search or "").strip()

        users = User.objects.select_related("profile", "employee")

        if search:
            terms = [t for t in search.split() if t]
            if len(terms) >= 2:
                first = terms[0]
                last = terms[-1]
                full_name_q = (
                    Q(first_name__icontains=first) & Q(last_name__icontains=last)
                ) | (Q(first_name__icontains=last) & Q(last_name__icontains=first))
            else:
                full_name_q = Q(first_name__icontains=search) | Q(
                    last_name__icontains=search
                )

            users = users.filter(
                Q(employee__name__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | full_name_q
                | Q(username__icontains=search)
                | Q(email__icontains=search)
            ).distinct()

        users = users.prefetch_related(
            Prefetch(
                "employee__leave_requests",
                queryset=leaves_qs,
                to_attr="prefetched_leaves",
            )
        )
        return Response(
            AllUsersDetailSerializer(users, many=True).data, status=status.HTTP_200_OK
        )


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
            return Response(
                {"error": ["User not found"]}, status=status.HTTP_404_NOT_FOUND
            )

        if getattr(user, "employee", None):
            user.employee.prefetched_leaves = list(
                LeaveRequest.objects.filter(employee=user.employee)
                .prefetch_related("days")
                .order_by("-applied_at", "-id")
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
        return Response(
            EmployeeDetailSerializer(employees, many=True).data,
            status=status.HTTP_200_OK,
        )


class EmployeesByRoleView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="List employee users (profile role=EMPLOYEE) with prefetched leave requests.",
        parameters=[
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
            )
        ],
        responses={200: AllUsersDetailSerializer(many=True)},
    )
    def get(self, request):
        leaves_qs = LeaveRequest.objects.order_by(
            "-applied_at", "-id"
        ).prefetch_related("days")
        params = request.GET
        raw_search = (
            (params.get("search") or "")
            or (params.get("q") or "")
            or (params.get("name") or "")
        )
        search = (raw_search or "").strip()

        users = User.objects.filter(profile__role="EMPLOYEE").select_related(
            "profile", "employee"
        )

        if search:
            terms = [t for t in search.split() if t]
            if len(terms) >= 2:
                first = terms[0]
                last = terms[-1]
                full_name_q = (
                    Q(first_name__icontains=first) & Q(last_name__icontains=last)
                ) | (Q(first_name__icontains=last) & Q(last_name__icontains=first))
            else:
                full_name_q = Q(first_name__icontains=search) | Q(
                    last_name__icontains=search
                )

            users = users.filter(
                Q(employee__name__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | full_name_q
                | Q(username__icontains=search)
                | Q(email__icontains=search)
            ).distinct()

        users = users.prefetch_related(
            Prefetch(
                "employee__leave_requests",
                queryset=leaves_qs,
                to_attr="prefetched_leaves",
            )
        )
        return Response(
            AllUsersDetailSerializer(users, many=True).data, status=status.HTTP_200_OK
        )


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
            return Response(
                {"error": ["Employee not found"]}, status=status.HTTP_404_NOT_FOUND
            )

        return Response(EmployeeDetailSerializer(emp).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Update employee by employee_id (PATCH).",
        request=AdminEmployeeUpdateSerializer,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
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
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="page_size",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="pageSize",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="per_page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="limit",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
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

        page = _first_present_int(
            ("page", "current_page", "currentPage", "pageNumber"), 1
        )
        page_size = _first_present_int(
            ("page_size", "pageSize", "per_page", "perPage", "limit"), 10
        )

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
        description="Admin pending requests summary + recent requests.",
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        base_qs = AdminRequestServices.unfiltered_queryset().order_by(
            "-applied_at", "-id"
        )

        recent_qs = base_qs[:7]
        recent_requests = [serialize_request_for_frontend(lr) for lr in recent_qs]

        pending_qs = base_qs.annotate(_s=_norm_status_expr("status")).filter(
            _s=LeaveRequest.STATUS_PENDING
        )

        pending_requests = [serialize_request_for_frontend(lr) for lr in pending_qs]

        today = timezone.localdate()
        summary = {
            "team_members": Profile.objects.filter(
                role="EMPLOYEE", user__employee__isnull=False
            ).count(),
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

        return Response(
            {
                "summary": summary,
                "pending_requests": pending_requests,
                "recents": recent_requests,
            },
            status=status.HTTP_200_OK,
        )


class AdminTopLeaveTakersView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description=(
            "Top leave takers (approved sick leaves only). "
            "Supports month-wise filtering via ?month=MM&year=YYYY or ?month=YYYY-MM."
        ),
        parameters=[
            OpenApiParameter(
                name="month",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Month selector: MM or YYYY-MM.",
            ),
            OpenApiParameter(
                name="year",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Year for month-wise mode (e.g. ?month=4&year=2026).",
            ),
            OpenApiParameter(
                name="limit",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Max rows to return (default 5, max 20).",
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        params = request.GET.copy()
        params["leave_type"] = "SICK"

        try:
            limit = int(params.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))

        top_leave_takers = AdminRequestServices.top_leave_takers(params, limit=limit)

        month_raw = str(params.get("month", "")).strip()
        year_raw = str(params.get("year", "")).strip()
        response_data = {
            "top_leave_takers": top_leave_takers,
            "limit": limit,
        }

        if month_raw or year_raw:
            try:
                year, month = parse_kpi_month_year_params(params)
                response_data["month"] = f"{year:04d}-{month:02d}"
            except Exception:
                # Defensive fallback; service gracefully handles invalid month/year too.
                pass

        return Response(response_data, status=status.HTTP_200_OK)


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
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
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
                return Response(
                    {"error": ["User not found"]}, status=status.HTTP_404_NOT_FOUND
                )

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
                        if getattr(user, "employee", None)
                        and callable(getattr(user.employee, "is_on_probation", None))
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
                return Response(
                    {"error": ["user_id is required"]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                user = User.objects.select_for_update().get(id=int(user_id))
            except (User.DoesNotExist, ValueError, TypeError):
                return Response(
                    {"error": ["User not found"]}, status=status.HTTP_404_NOT_FOUND
                )

            if request.user and user.id == request.user.id:
                return Response(
                    {"error": ["You cannot delete your own account."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if user.is_staff or user.is_superuser:
                return Response(
                    {"error": ["Admin accounts cannot be deleted."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                target_profile = user.profile
            except ObjectDoesNotExist:
                target_profile = None

            if getattr(target_profile, "role", None) == Profile.ROLE_ADMIN:
                return Response(
                    {"error": ["Admin accounts cannot be deleted."]},
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
        return Response(
            LeavePolicySettingsSerializer(settings_obj).data, status=status.HTTP_200_OK
        )

    @extend_schema(
        description="Update leave policy settings (partial).",
        request=LeavePolicySettingsSerializer,
        responses={200: LeavePolicySettingsSerializer, 400: OpenApiTypes.OBJECT},
    )
    def patch(self, request):
        settings_obj = get_leave_policy_settings()
        ser = LeavePolicySettingsSerializer(
            settings_obj, data=request.data, partial=True
        )
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data, status=status.HTTP_200_OK)


class AdminHolidaysSettingsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description=(
            "List holidays grouped by name/description. "
            "Multi-day holidays appear as one entry with dates array. "
            "Use id (first/min ID) for editing/deleting."
        ),
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        from form_app.models import Holiday

        qs = Holiday.objects.order_by("name", "description", "is_active", "date")

        # Group holidays by (name, description, is_active)
        grouped = {}
        for holiday in qs:
            key = (holiday.name, holiday.description, holiday.is_active)
            if key not in grouped:
                grouped[key] = {
                    "id": holiday.id,  # Use first (min) ID
                    "name": holiday.name,
                    "description": holiday.description,
                    "is_active": holiday.is_active,
                    "dates": [],
                    "updated_at": holiday.updated_at.isoformat(),
                }
            grouped[key]["dates"].append(holiday.date.isoformat())

        result = list(grouped.values())
        return Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        description=(
            "Create holiday(s). Prefer sending `dates` as a list of dates "
            "(single-day can be one-item list) to bulk-create multiple Holiday rows. "
            "Legacy single `date` is accepted and normalized to `dates`. "
            "Bulk mode skips dates that already exist."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            201: AdminHolidaySerializer,
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        from form_app.models import Holiday

        payload = request.data or {}

        # Normalize legacy single-date payload to dates[] so clients can use one format.
        if payload.get("date") is not None and payload.get("dates") is None:
            payload = {**payload, "dates": [payload.get("date")]}

        # Bulk mode: dates[]
        if "dates" in payload and payload.get("dates") is not None:
            bulk_ser = AdminHolidayBulkCreateSerializer(data=payload)
            bulk_ser.is_valid(raise_exception=True)

            raw_dates = bulk_ser.validated_data["dates"]
            dates = sorted(set(raw_dates))
            name = bulk_ser.validated_data["name"]
            description = bulk_ser.validated_data.get("description", "") or ""
            is_active = bool(bulk_ser.validated_data.get("is_active", True))

            existing = set(
                Holiday.objects.filter(date__in=dates).values_list("date", flat=True)
            )
            to_create = [d for d in dates if d not in existing]
            if to_create:
                Holiday.objects.bulk_create(
                    [
                        Holiday(
                            date=d,
                            name=name,
                            description=description,
                            is_active=is_active,
                        )
                        for d in to_create
                    ]
                )

            # Fetch created holidays with IDs
            created_holiday_data = []
            if to_create:
                created_holidays = Holiday.objects.filter(date__in=to_create).order_by(
                    "date"
                )
                created_holiday_data = [
                    {"id": h.id, "date": h.date.isoformat()} for h in created_holidays
                ]

            skipped_dates = [d.isoformat() for d in dates if d in existing]

            return Response(
                {
                    "status": "success",
                    "created_count": len(created_holiday_data),
                    "skipped_count": len(skipped_dates),
                    "created": created_holiday_data,
                    "skipped_dates": skipped_dates,
                },
                status=status.HTTP_200_OK,
            )

        # Single mode: date
        ser = AdminHolidayUpsertSerializer(data=payload)
        ser.is_valid(raise_exception=True)
        holiday = ser.save()
        return Response(
            AdminHolidaySerializer(holiday).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        description=(
            "Update a holiday by body. Provide `id` (or `holiday_id`) plus any fields to change. "
            "Common usage: toggle `is_active` from Admin UI Settings."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: AdminHolidaySerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def patch(self, request):
        from form_app.models import Holiday

        raw_id = request.data.get("id") or request.data.get("holiday_id")
        if not raw_id:
            return Response(
                {"error": ["id is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {"error": ["Invalid id"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday = Holiday.objects.select_for_update().get(id=holiday_id)
        except Holiday.DoesNotExist:
            return Response(
                {"error": ["Holiday not found"]},
                status=status.HTTP_404_NOT_FOUND,
            )

        ser = AdminHolidayUpsertSerializer(holiday, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        holiday = ser.save()
        return Response(AdminHolidaySerializer(holiday).data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Delete a holiday by body (holiday_id or id).",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def delete(self, request):
        from form_app.models import Holiday

        raw_id = request.data.get("holiday_id") or request.data.get("id")
        if not raw_id:
            return Response(
                {"error": ["holiday_id is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {"error": ["Invalid holiday_id"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday = Holiday.objects.select_for_update().get(id=holiday_id)
        except Holiday.DoesNotExist:
            return Response(
                {"error": ["Holiday not found"]},
                status=status.HTTP_404_NOT_FOUND,
            )

        holiday.delete()
        return Response(
            {"status": "success", "message": "Holiday deleted successfully"},
            status=status.HTTP_200_OK,
        )


class AdminHolidayDeleteByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Delete a holiday by body (holiday_id or id).",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        from form_app.models import Holiday

        raw_id = request.data.get("holiday_id") or request.data.get("id")
        if not raw_id:
            return Response(
                {"error": ["holiday_id is required"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {"error": ["Invalid holiday_id"]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            holiday = Holiday.objects.select_for_update().get(id=holiday_id)
        except Holiday.DoesNotExist:
            return Response(
                {"error": ["Holiday not found"]},
                status=status.HTTP_404_NOT_FOUND,
            )

        holiday.delete()
        return Response(
            {"status": "success", "message": "Holiday deleted successfully"},
            status=status.HTTP_200_OK,
        )

    @extend_schema(exclude=True)
    def delete(self, request):
        return self.post(request)


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

        return Response(
            EmployeeRenewalScheduleSerializer(employees, many=True).data,
            status=status.HTTP_200_OK,
        )

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
        return Response(
            EmployeeRenewalScheduleSerializer(emp).data, status=status.HTTP_200_OK
        )


class ApproveLeaveByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description="Approve leave by formID (or form_id) in request body.",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        form_id = request.data.get("formID") or request.data.get("form_id")
        message = (request.data.get("message") or "").strip() or None

        if not form_id:
            return Response(
                {"error": ["formID is required"]}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            lr = decide_leave(
                leave_id=int(form_id),
                new_status=LeaveRequest.STATUS_APPROVED,
                message=message,
            )
        except (ValueError, TypeError):
            return Response(
                {"error": ["Invalid formID"]}, status=status.HTTP_400_BAD_REQUEST
            )
        except LeaveRequest.DoesNotExist:
            return Response(
                {"error": ["Leave request not found"]}, status=status.HTTP_404_NOT_FOUND
            )

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
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        form_id = request.data.get("formID") or request.data.get("form_id")
        message = (request.data.get("message") or "").strip()

        if not form_id:
            return Response(
                {"error": ["formID is required"]}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            lr = decide_leave(
                leave_id=int(form_id),
                new_status=LeaveRequest.STATUS_REJECTED,
                message=message,
            )
        except (ValueError, TypeError):
            return Response(
                {"error": ["Invalid formID"]}, status=status.HTTP_400_BAD_REQUEST
            )
        except LeaveRequest.DoesNotExist:
            return Response(
                {"error": ["Leave request not found"]}, status=status.HTTP_404_NOT_FOUND
            )

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


class AdminLeaveDeleteByBodyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description=(
            "Delete a leave request (LeaveRequest) by body. "
            "This intentionally uses Django-native deletion semantics (same as Django Admin delete), "
            "including cascade deletes and model delete signals."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def post(self, request):
        raw_id = (
            request.data.get("formID")
            or request.data.get("form_id")
            or request.data.get("leave_id")
            or request.data.get("id")
        )

        if not raw_id:
            return Response(
                {"error": ["formID is required"]}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            leave_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {"error": ["Invalid formID"]}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            lr = LeaveRequest.objects.select_for_update().get(id=leave_id)
        except LeaveRequest.DoesNotExist:
            return Response(
                {"error": ["Leave request not found"]},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            lr.delete()
        except ProtectedError:
            return Response(
                {
                    "status": "error",
                    "message": "Leave request cannot be deleted because related records exist.",
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "status": "success",
                "message": "Leave request deleted successfully",
                "formID": leave_id,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(exclude=True)
    def delete(self, request):
        return self.post(request)


class AdminRefreshDailyLeaveMessageView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(
        description=(
            "Manually refresh the employee daily leave message in Discord "
            "(same upsert logic used by cron)."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        from discord_app.models import DiscordDailyMessage
        from discord_app.services import (
            _active_today_qs,
            upsert_employee_on_leave_today_message,
        )

        today = timezone.localdate()

        if today.weekday() in (5, 6):
            return Response(
                {
                    "status": "skipped",
                    "reason": "weekend",
                    "date": str(today),
                },
                status=status.HTTP_200_OK,
            )

        existing_record = (
            DiscordDailyMessage.objects.filter(
                key=DiscordDailyMessage.KEY_EMPLOYEE_ON_LEAVE_TODAY,
                target_date=today,
            )
            .order_by("-id")
            .first()
        )

        eligible_count = _active_today_qs(today).count()
        ok = upsert_employee_on_leave_today_message(
            target_date=today,
            create_if_missing=True,
        )

        if not ok:
            return Response(
                {
                    "status": "failed",
                    "message": "Could not refresh daily leave message",
                    "date": str(today),
                    "eligible_count": eligible_count,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        latest_record = (
            DiscordDailyMessage.objects.filter(
                key=DiscordDailyMessage.KEY_EMPLOYEE_ON_LEAVE_TODAY,
                target_date=today,
            )
            .order_by("-id")
            .first()
        )

        logger.info(
            "Manual daily leave refresh by user_id=%s date=%s eligible_count=%s",
            getattr(request.user, "id", None),
            today,
            eligible_count,
        )

        return Response(
            {
                "status": "updated" if existing_record else "created",
                "date": str(today),
                "eligible_count": eligible_count,
                "channel_id": getattr(latest_record, "channel_id", None),
                "message_id": getattr(latest_record, "message_id", None),
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(exclude=True)
    def get(self, request):
        return self.post(request)
