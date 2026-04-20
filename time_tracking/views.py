from __future__ import annotations

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import TimeEntry, TimeProject
from .permissions import can_manage_time_projects
from .serializers import (
    TimeEntrySerializer,
    TimeEntryStartSerializer,
    TimeEntryStopSerializer,
    TimeProjectSerializer,
)


def _parse_dt(value: str | None):
    if not value:
        return None
    dt = parse_datetime(value)
    return dt


def _project_queryset_for_user(user, include_archived: bool = False):
    qs = TimeProject.objects.all()
    if can_manage_time_projects(user):
        if not include_archived:
            qs = qs.filter(is_archived=False)
    else:
        qs = qs.filter(is_archived=False)
    return qs.order_by("is_archived", "name", "id")


class TimeProjectListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: TimeProjectSerializer(many=True)},
        tags=["time_tracking"],
    )
    def get(self, request):
        include_archived = str(request.query_params.get("include_archived", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        qs = _project_queryset_for_user(
            request.user, include_archived=include_archived
        )
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
        obj = TimeProject.objects.create(user=request.user, **ser.validated_data)
        return Response(
            TimeProjectSerializer(obj).data, status=status.HTTP_201_CREATED
        )


class TimeProjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk: int) -> TimeProject:
        return TimeProject.objects.get(pk=pk)

    @extend_schema(responses={200: TimeProjectSerializer}, tags=["time_tracking"])
    def get(self, request, pk: int):
        obj = self._get(request, pk)
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
        obj = self._get(request, pk)
        ser = TimeProjectSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        for k, v in ser.validated_data.items():
            setattr(obj, k, v)
        obj.save()
        return Response(TimeProjectSerializer(obj).data)

    @extend_schema(responses={204: None}, tags=["time_tracking"])
    def delete(self, request, pk: int):
        if not can_manage_time_projects(request.user):
            return Response(
                {"detail": "Only admin/manager can manage time projects."},
                status=status.HTTP_403_FORBIDDEN,
            )
        obj = self._get(request, pk)
        obj.delete()
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
        ],
        responses={200: TimeEntrySerializer(many=True)},
        tags=["time_tracking"],
    )
    def get(self, request):
        qs = TimeEntry.objects.filter(user=request.user).select_related("project")
        dt_from = _parse_dt(request.query_params.get("from"))
        dt_to = _parse_dt(request.query_params.get("to"))
        if dt_from:
            qs = qs.filter(started_at__gte=dt_from)
        if dt_to:
            qs = qs.filter(started_at__lte=dt_to)
        qs = qs.order_by("-started_at", "-id")[:500]
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
        return TimeEntry.objects.select_related("project").get(pk=pk, user=request.user)

    @extend_schema(responses={200: TimeEntrySerializer}, tags=["time_tracking"])
    def get(self, request, pk: int):
        obj = self._get(request, pk)
        return Response(TimeEntrySerializer(obj, context={"request": request}).data)

    @extend_schema(
        request=TimeEntrySerializer,
        responses={200: TimeEntrySerializer},
        tags=["time_tracking"],
    )
    def patch(self, request, pk: int):
        obj = self._get(request, pk)
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
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TimeEntrySerializer(obj, context={"request": request}).data)

    @extend_schema(responses={204: None}, tags=["time_tracking"])
    def delete(self, request, pk: int):
        obj = self._get(request, pk)
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
