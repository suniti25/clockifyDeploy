from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db.models import (
    Case,
    DurationField,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Now
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from user_app.models import Employee, Project

from . import services
from .models import TimeEntry
from .permissions import can_manage_time_projects, can_view_all_time_entries
from .serializers import (
    TimeEntrySerializer,
    TimeEntryStartSerializer,
    TimeEntryStopSerializer,
    TimeProjectSerializer,
)


def _parse_dt(value: str | None):
    if not value:
        return None
    dt = parse_datetime(value.strip())
    if dt is None:
        raise ValueError("Invalid datetime value; use ISO 8601 format.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _project_queryset_for_user(user, include_inactive: bool = False):
    qs = Project.objects.all()
    if can_manage_time_projects(user):
        if not include_inactive:
            qs = qs.filter(is_active=True)
    else:
        qs = qs.filter(is_active=True)
    return qs.order_by("-is_active", "name", "id")


class TimeProjectListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "include_inactive",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Include inactive projects for admin/manager",
            ),
        ],
        responses={200: TimeProjectSerializer(many=True)},
        tags=["time_tracking"],
    )
    def get(self, request):
        include_inactive = str(request.query_params.get("include_inactive", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        qs = _project_queryset_for_user(request.user, include_inactive=include_inactive)
        return Response(TimeProjectSerializer(qs, many=True).data)

    @extend_schema(
        request=TimeProjectSerializer,
        responses={201: TimeProjectSerializer},
        tags=["time_tracking"],
    )
    def post(self, request):
        if not can_manage_time_projects(request.user):
            return Response(
                {"detail": "Only admin/manager can manage time projects."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = TimeProjectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = Project.objects.create(**ser.validated_data)
        return Response(
            TimeProjectSerializer(obj).data, status=status.HTTP_201_CREATED
        )


class TimeProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk: int, *, include_inactive: bool) -> Project:
        qs = _project_queryset_for_user(request.user, include_inactive=include_inactive)
        return get_object_or_404(qs, pk=pk)

    @extend_schema(responses={200: TimeProjectSerializer}, tags=["time_tracking"])
    def get(self, request, pk: int):
        include_inactive = str(request.query_params.get("include_inactive", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        obj = self._get(request, pk, include_inactive=include_inactive)
        return Response(TimeProjectSerializer(obj).data)

    @extend_schema(
        request=TimeProjectSerializer,
        responses={200: TimeProjectSerializer},
        tags=["time_tracking"],
    )
    def patch(self, request, pk: int):
        if not can_manage_time_projects(request.user):
            return Response(
                {"detail": "Only admin/manager can manage time projects."},
                status=status.HTTP_403_FORBIDDEN,
            )
        obj = self._get(request, pk, include_inactive=True)
        ser = TimeProjectSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        was_active = obj.is_active
        for k, v in ser.validated_data.items():
            setattr(obj, k, v)
        obj.save()
        if was_active and not obj.is_active:
            Employee.objects.filter(current_project=obj.name).update(current_project="")
        return Response(TimeProjectSerializer(obj).data)

    @extend_schema(responses={204: None}, tags=["time_tracking"])
    def delete(self, request, pk: int):
        if not can_manage_time_projects(request.user):
            return Response(
                {"detail": "Only admin/manager can manage time projects."},
                status=status.HTTP_403_FORBIDDEN,
            )
        obj = self._get(request, pk, include_inactive=True)
        if obj.is_active:
            obj.is_active = False
            obj.save(update_fields=["is_active"])
            Employee.objects.filter(current_project=obj.name).update(current_project="")
        return Response(status=status.HTTP_204_NO_CONTENT)


class TimeEntryListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "from",
                OpenApiTypes.DATETIME,
                OpenApiParameter.QUERY,
                description="Filter entries with started_at >= from (ISO 8601)",
            ),
            OpenApiParameter(
                "to",
                OpenApiTypes.DATETIME,
                OpenApiParameter.QUERY,
                description="Filter entries with started_at <= to (ISO 8601)",
            ),
            OpenApiParameter(
                "limit",
                OpenApiTypes.INT,
                OpenApiParameter.QUERY,
                description="Limit number of entries returned (max 500)",
            ),
        ],
        responses={200: TimeEntrySerializer(many=True)},
        tags=["time_tracking"],
    )
    def get(self, request):
        qs = TimeEntry.objects.filter(user=request.user).select_related("project", "user")
        try:
            dt_from = _parse_dt(request.query_params.get("from"))
            dt_to = _parse_dt(request.query_params.get("to"))
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if dt_from:
            qs = qs.filter(started_at__gte=dt_from)
        if dt_to:
            qs = qs.filter(started_at__lte=dt_to)
        try:
            limit = int(request.query_params.get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        limit = max(1, min(limit, 500))
        qs = qs.order_by("-started_at", "-id")[:limit]
        return Response(
            TimeEntrySerializer(qs, many=True, context={"request": request}).data
        )

    @extend_schema(
        request=TimeEntrySerializer,
        responses={201: TimeEntrySerializer},
        tags=["time_tracking"],
    )
    def post(self, request):
        ser = TimeEntrySerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response(
            TimeEntrySerializer(obj, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TimeEntryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk: int) -> TimeEntry:
        qs = TimeEntry.objects.select_related("project", "user").filter(pk=pk)
        if not can_view_all_time_entries(request.user):
            qs = qs.filter(user=request.user)
        return qs.first()

    @extend_schema(responses={200: TimeEntrySerializer}, tags=["time_tracking"])
    def get(self, request, pk: int):
        obj = self._get(request, pk)
        if obj is None:
            return Response({"detail": "Entry not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(TimeEntrySerializer(obj, context={"request": request}).data)

    @extend_schema(
        request=TimeEntrySerializer,
        responses={200: TimeEntrySerializer},
        tags=["time_tracking"],
    )
    def patch(self, request, pk: int):
        obj = self._get(request, pk)
        if obj is None:
            return Response({"detail": "Entry not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = TimeEntrySerializer(
            obj,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)
        service_kwargs = {}
        if "project" in ser.validated_data:
            service_kwargs["project"] = ser.validated_data.get("project")
        if "description" in ser.validated_data:
            service_kwargs["description"] = ser.validated_data.get("description")
        if "started_at" in ser.validated_data:
            service_kwargs["started_at"] = ser.validated_data.get("started_at")
        if "ended_at" in ser.validated_data:
            service_kwargs["ended_at"] = ser.validated_data.get("ended_at")

        try:
            obj = services.update_entry(
                user=request.user,
                entry=obj,
                **service_kwargs,
            )
        except (ValueError, ValidationError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TimeEntrySerializer(obj, context={"request": request}).data)

    @extend_schema(responses={204: None}, tags=["time_tracking"])
    def delete(self, request, pk: int):
        obj = self._get(request, pk)
        if obj is None:
            return Response({"detail": "Entry not found."}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TimeEntryContinueView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={201: TimeEntrySerializer}, tags=["time_tracking"])
    def post(self, request, pk: int):
        try:
            source = TimeEntry.objects.select_related("project").get(
                pk=pk, user=request.user
            )
        except TimeEntry.DoesNotExist:
            return Response({"detail": "Entry not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            entry = services.continue_entry(user=request.user, source_entry=source)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            TimeEntrySerializer(entry, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TimeEntryDuplicateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={201: TimeEntrySerializer}, tags=["time_tracking"])
    def post(self, request, pk: int):
        try:
            source = TimeEntry.objects.select_related("project").get(
                pk=pk, user=request.user
            )
        except TimeEntry.DoesNotExist:
            return Response({"detail": "Entry not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            entry = services.duplicate_entry(user=request.user, source_entry=source)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            TimeEntrySerializer(entry, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TimeEntryRunningView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: TimeEntrySerializer}, tags=["time_tracking"])
    def get(self, request):
        running = services.get_running_entry(request.user)
        if running is None:
            return Response(None)
        return Response(TimeEntrySerializer(running, context={"request": request}).data)


class TimeEntryLatestByUserView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "ordering",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Sort field: user_name, project_name, started_at, duration_seconds, is_running",
            ),
            OpenApiParameter(
                "direction",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Sort direction: asc or desc (default asc)",
            ),
            OpenApiParameter(
                "page",
                OpenApiTypes.INT,
                OpenApiParameter.QUERY,
                description="Page number (default 1)",
            ),
            OpenApiParameter(
                "page_size",
                OpenApiTypes.INT,
                OpenApiParameter.QUERY,
                description="Items per page (default 20, max 100)",
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
        tags=["time_tracking"],
    )
    def get(self, request):
        if not can_view_all_time_entries(request.user):
            return Response(
                {"detail": "Only admin/manager can view all users' latest entries."},
                status=status.HTTP_403_FORBIDDEN,
            )
        latest_entry_subquery = (
            TimeEntry.objects.filter(user_id=OuterRef("user_id"))
            .order_by("-started_at", "-id")
            .values("id")[:1]
        )
        qs = (
            TimeEntry.objects.filter(id=Subquery(latest_entry_subquery))
            .select_related("project", "user")
            .annotate(
                user_name_sort=Case(
                    When(
                        user__first_name="",
                        then=F("user__username"),
                    ),
                    default=F("user__first_name"),
                ),
                project_name_sort=Case(
                    When(project__name__isnull=True, then=Value("No project")),
                    default=F("project__name"),
                ),
                is_running_sort=Case(
                    When(ended_at__isnull=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
                duration_sort=ExpressionWrapper(
                    Coalesce(F("ended_at"), Now()) - F("started_at"),
                    output_field=DurationField(),
                ),
            )
        )

        ordering = (request.query_params.get("ordering") or "user_name").strip()
        direction = (request.query_params.get("direction") or "asc").strip().lower()
        order_prefix = "-" if direction == "desc" else ""
        order_map = {
            "user_name": f"{order_prefix}user_name_sort",
            "project_name": f"{order_prefix}project_name_sort",
            "started_at": f"{order_prefix}started_at",
            "duration_seconds": f"{order_prefix}duration_sort",
            "is_running": f"{order_prefix}is_running_sort",
        }
        qs = qs.order_by(
            order_map.get(ordering, f"{order_prefix}user_name_sort"),
            "-started_at",
            "-id",
        )

        try:
            page = int(request.query_params.get("page", 1))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(request.query_params.get("page_size", 20))
        except (TypeError, ValueError):
            page_size = 20
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        total_count = qs.count()
        start = (page - 1) * page_size
        end = start + page_size
        entries = qs[start:end]

        return Response(
            {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "results": TimeEntrySerializer(
                    entries, many=True, context={"request": request}
                ).data,
            }
        )


class TimeEntryRecentView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "limit",
                OpenApiTypes.INT,
                OpenApiParameter.QUERY,
                description="Recent entry count (default 8, max 50)",
            ),
        ],
        responses={200: TimeEntrySerializer(many=True)},
        tags=["time_tracking"],
    )
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 8))
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 50))
        entries = (
            TimeEntry.objects.filter(user=request.user)
            .select_related("project", "user")
            .order_by("-started_at", "-id")[:limit]
        )
        return Response(
            TimeEntrySerializer(entries, many=True, context={"request": request}).data
        )


class TimeEntryStartView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=TimeEntryStartSerializer,
        responses={201: TimeEntrySerializer},
        tags=["time_tracking"],
    )
    def post(self, request):
        ser = TimeEntryStartSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        project = ser.validated_data.get("project")
        project_id = project.pk if project else None
        try:
            entry = services.start_timer(
                user=request.user,
                project_id=project_id,
                description=ser.validated_data.get("description") or "",
                started_at=ser.validated_data.get("started_at") or timezone.now(),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            TimeEntrySerializer(entry, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class TimeEntryStopView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=TimeEntryStopSerializer,
        responses={200: TimeEntrySerializer},
        tags=["time_tracking"],
    )
    def post(self, request):
        ser = TimeEntryStopSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            entry = services.stop_running_timer(
                user=request.user,
                entry_id=ser.validated_data.get("entry_id"),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TimeEntrySerializer(entry, context={"request": request}).data)
