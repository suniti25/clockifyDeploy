from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from discord_app.services import send_leave_request_to_admin
from form_app.helpers import (
    compute_leave_days_for_payload,
    compute_paid_status,
    compute_paid_unpaid_split,
    compute_paid_unpaid_split_for_request,
    display_is_paid,
    validate_leave_application_inputs,
)
from form_app.models import LeaveRequest, LeaveRequestDay, Holiday
from user_app.helpers import _get_profile_and_employee

logger = logging.getLogger(__name__)


def _collect_reapply_ancestor_ids(leave_request: LeaveRequest) -> list[int]:
    """Return all ancestor leave IDs in a reapply chain (nearest to oldest)."""
    ancestor_ids: list[int] = []
    seen: set[int] = set()
    current_id = getattr(leave_request, "reapplied_from_id", None)

    while current_id and current_id not in seen:
        seen.add(current_id)
        ancestor_ids.append(int(current_id))
        current_id = (
            LeaveRequest.objects.filter(id=current_id)
            .values_list("reapplied_from_id", flat=True)
            .first()
        )

    return ancestor_ids


def _parse_iso_date(value, field_name: str):
    if value is None:
        return None, Response(
            {"error": [f"{field_name} is required"]}, status=status.HTTP_400_BAD_REQUEST
        )

    if isinstance(value, date):
        return value, None

    if isinstance(value, str):
        try:
            return date.fromisoformat(value), None
        except ValueError:
            return None, Response(
                {"error": [f"Invalid {field_name}. Use YYYY-MM-DD."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

    return None, Response(
        {"error": [f"Invalid {field_name}. Use YYYY-MM-DD."]},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _validation_error_response(exc: serializers.ValidationError) -> Response:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, list):
        error_message = str(detail[0]) if detail else str(exc)
    elif isinstance(detail, dict):
        flattened = []
        for value in detail.values():
            if isinstance(value, list):
                flattened.extend(str(item) for item in value)
            else:
                flattened.append(str(value))
        error_message = flattened[0] if flattened else str(exc)
    elif detail is None:
        error_message = str(exc)
    else:
        error_message = str(detail)
    return Response({"error": error_message}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    description="Apply a leave request (creates a PENDING request and notifies admin via Discord).",
    request=OpenApiTypes.OBJECT,
    responses={
        201: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        401: OpenApiTypes.OBJECT,
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_leave(request):
    profile, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    data = request.data or {}

    start_date_val = data.get("start_date")
    end_date_val = data.get("end_date")
    dates_val = data.get("dates")
    leave_type = data.get("leave_type")
    reason = data.get("reason", "") or ""

    session = data.get("session") or data.get("half_day") or "FULL"

    dates_list = None
    if dates_val is not None:
        # Manual mode: expect a list of ISO date strings.
        if start_date_val is not None or end_date_val is not None:
            return Response(
                {
                    "error": [
                        "Send either start_date/end_date (range) or dates[] (manual), not both."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(dates_val, list) or not dates_val:
            return Response(
                {"error": ["dates must be a non-empty list of YYYY-MM-DD strings."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed = []
        for i, v in enumerate(dates_val):
            d, resp = _parse_iso_date(v, f"dates[{i}]")
            if resp:
                return resp
            parsed.append(d)

        dates_list = parsed
        start_date = min(parsed)
        end_date = max(parsed)
    else:
        # Range mode
        start_date, resp = _parse_iso_date(start_date_val, "start_date")
        if resp:
            return resp
        end_date, resp = _parse_iso_date(end_date_val, "end_date")
        if resp:
            return resp

    try:
        normalized_session, normalized_leave_type = validate_leave_application_inputs(
            profile=profile,
            employee=employee,
            start_date=None if dates_list is not None else start_date,
            end_date=None if dates_list is not None else end_date,
            leave_type=leave_type,
            session=session,
            reason=reason,
            dates=dates_list,
        )
    except serializers.ValidationError as exc:
        return _validation_error_response(exc)

    payload = {
        "leave_type": normalized_leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "session": normalized_session,
        "start_session": normalized_session,
        "end_session": normalized_session,
        "reason": reason,
        "status": "PENDING",
    }

    if hasattr(LeaveRequest, "applied_at"):
        payload["applied_at"] = timezone.now()

    # Expand requested working dates for LeaveRequestDay creation.
    requested_working_dates = []
    if dates_list is not None:
        requested_working_dates = sorted(set(dates_list))
    else:
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5:
                requested_working_dates.append(cur)
            cur = cur + timedelta(days=1)

    # Compute leave days for paid/unpaid split.
    if dates_list is not None:
        per_day = 0.5 if normalized_session in {"AM", "PM"} else 1.0
        leave_days = float(len(requested_working_dates)) * float(per_day)
    else:
        leave_days = compute_leave_days_for_payload(employee=employee, payload=payload)

    paid_days, unpaid_days, remaining_paid_days = compute_paid_unpaid_split(
        employee=employee,
        leave_type=normalized_leave_type,
        leave_days=leave_days,
        start_date=start_date,
        end_date=end_date,
    )

    payload["is_paid"] = bool(
        compute_paid_status(
            employee=employee,
            leave_type=normalized_leave_type,
            leave_days=leave_days,
            start_date=start_date,
            end_date=end_date,
        )
    )

    with transaction.atomic():
        lr = LeaveRequest.objects.create(employee=employee, **payload)
        if requested_working_dates:
            LeaveRequestDay.objects.bulk_create(
                [
                    LeaveRequestDay(
                        leave_request=lr,
                        date=d,
                        session=normalized_session,
                    )
                    for d in requested_working_dates
                ]
            )

    try:
        send_leave_request_to_admin(lr)
    except Exception:
        logger.exception(
            "Failed to send leave request to Discord for leave_id=%s", lr.id
        )

    return Response(
        {
            "message": "Leave applied successfully",
            "id": lr.id,
            "leave_type": lr.leave_type,
            "start_date": lr.start_date.isoformat(),
            "end_date": lr.end_date.isoformat(),
            "dates": [d.isoformat() for d in (requested_working_dates or [])],
            "session": lr.session,
            "status": lr.status,
            "is_paid": display_is_paid(lr.leave_type, getattr(lr, "is_paid", None)),
            "days": float(lr.total_days()),
            "paid_days": float(paid_days),
            "unpaid_days": float(unpaid_days),
            "remaining_paid_days_before_request": float(remaining_paid_days),
            "reapplied_from": getattr(lr, "reapplied_from_id", None),
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    description="Get current user's leave requests (latest first).",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_requests(request):
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return Response([], status=status.HTTP_200_OK)

    qs = (
        LeaveRequest.objects.filter(employee=employee)
        .prefetch_related("days")
        .order_by("-id")
    )

    results = []
    for lr in qs[:200]:
        created_at = getattr(lr, "applied_at", None) or getattr(lr, "created_at", None)
        paid_days, unpaid_days = compute_paid_unpaid_split_for_request(
            employee=employee, req=lr
        )

        day_rows = list(lr.days.all()) if hasattr(lr, "days") else []
        results.append(
            {
                "id": lr.id,
                "leave_type": lr.leave_type,
                "start_date": lr.start_date.isoformat() if lr.start_date else None,
                "end_date": lr.end_date.isoformat() if lr.end_date else None,
                "dates": [d.date.isoformat() for d in day_rows],
                "session": getattr(lr, "session", "FULL"),
                "status": lr.status,
                "is_paid": display_is_paid(lr.leave_type, getattr(lr, "is_paid", None)),
                "days": float(lr.total_days()) if hasattr(lr, "total_days") else None,
                "paid_days": float(paid_days),
                "unpaid_days": float(unpaid_days),
                "reason": getattr(lr, "reason", "") or "",
                "submitted_at": created_at.isoformat() if created_at else None,
                "discord_message_id": getattr(lr, "discord_message_id", None),
                "reapplied_from": getattr(lr, "reapplied_from_id", None),
            }
        )

    return Response(results, status=status.HTTP_200_OK)


@extend_schema(
    description=(
        "Update a leave by request body. "
        "If original is PENDING -> updates it, else creates a new PENDING re-apply linked to original."
    ),
    request=OpenApiTypes.OBJECT,
    responses={
        200: OpenApiTypes.OBJECT,
        201: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        401: OpenApiTypes.OBJECT,
        404: OpenApiTypes.OBJECT,
    },
)
@extend_schema(
    description="Get all active holidays.",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def get_holidays(request):
    holidays = Holiday.objects.filter(is_active=True).order_by(
        "name", "description", "date"
    )

    grouped = {}
    for holiday in holidays:
        key = (holiday.name, holiday.description, holiday.is_active)
        item = grouped.get(key)
        if item is None:
            item = {
                "id": holiday.id,
                "date": [],
                "name": holiday.name,
                "description": holiday.description,
                "is_active": holiday.is_active,
                "updated_at": holiday.updated_at,
            }
            grouped[key] = item

        item["date"].append(holiday.date.isoformat())
        if holiday.id < item["id"]:
            item["id"] = holiday.id
        if holiday.updated_at > item["updated_at"]:
            item["updated_at"] = holiday.updated_at

    result = list(grouped.values())
    for item in result:
        item["updated_at"] = item["updated_at"].isoformat()

    return Response(result, status=status.HTTP_200_OK)


@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def update_leave_by_body(request):
    profile, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    data = request.data or {}

    leave_id = data.get("id") or data.get("form_id")
    if not leave_id:
        return Response(
            {"error": ["id is required"]}, status=status.HTTP_400_BAD_REQUEST
        )

    form_payload = data.get("form_payload")
    payload = form_payload if isinstance(form_payload, dict) else data

    original = get_object_or_404(LeaveRequest, id=leave_id, employee=employee)
    if original.status == LeaveRequest.STATUS_VOIDED:
        return Response(
            {"error": ["Voided leave requests cannot be updated"]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    provided_start = "start_date" in payload
    provided_end = "end_date" in payload
    dates_val = payload.get("dates")

    start_date_val = payload.get("start_date")
    end_date_val = payload.get("end_date")
    leave_type = payload.get("leave_type", original.leave_type)
    reason = payload.get("reason", getattr(original, "reason", "")) or ""
    session = (
        payload.get("session") or payload.get("half_day") or original.session or "FULL"
    )

    # Prefer explicit dates[] (selective/manual mode). If absent, preserve existing
    # LeaveRequestDay rows when caller isn't changing the range.
    dates_list: list[date] | None = None
    existing_day_dates: list[date] = list(
        LeaveRequestDay.objects.filter(leave_request=original)
        .order_by("date")
        .values_list("date", flat=True)
    )

    if dates_val is not None:
        if provided_start or provided_end:
            return Response(
                {
                    "error": [
                        "Send either start_date/end_date (range) or dates[] (manual), not both."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(dates_val, list) or not dates_val:
            return Response(
                {"error": ["dates must be a non-empty list of YYYY-MM-DD strings."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed: list[date] = []
        for i, v in enumerate(dates_val):
            d, resp = _parse_iso_date(v, f"dates[{i}]")
            if resp:
                return resp
            parsed.append(d)
        dates_list = parsed

        start_date = min(parsed)
        end_date = max(parsed)
    else:
        # Range mode defaults
        if start_date_val is None:
            start_date_val = original.start_date
        if end_date_val is None:
            end_date_val = original.end_date

        start_date, resp = _parse_iso_date(start_date_val, "start_date")
        if resp:
            return resp
        end_date, resp = _parse_iso_date(end_date_val, "end_date")
        if resp:
            return resp

    # Decide which exact working dates to persist.
    if dates_list is not None:
        requested_working_dates = sorted(set(dates_list))
    elif existing_day_dates and not (provided_start or provided_end):
        # Caller didn't send a new range and didn't provide explicit dates[]:
        # preserve current selective dates.
        requested_working_dates = list(existing_day_dates)
        start_date = min(existing_day_dates)
        end_date = max(existing_day_dates)
        dates_list = list(existing_day_dates)
    else:
        requested_working_dates: list[date] = []
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5:
                requested_working_dates.append(cur)
            cur = cur + timedelta(days=1)

    try:
        normalized_session, normalized_leave_type = validate_leave_application_inputs(
            profile=profile,
            employee=employee,
            start_date=None if dates_list is not None else start_date,
            end_date=None if dates_list is not None else end_date,
            leave_type=leave_type,
            session=session,
            reason=reason,
            instance_id=original.id,
            allow_overlap_with_id=getattr(original, "reapplied_from_id", None),
            allow_overlap_with_ids=_collect_reapply_ancestor_ids(original),
            dates=dates_list,
        )
    except serializers.ValidationError as exc:
        return _validation_error_response(exc)

    if original.status == "PENDING":
        original.start_date = start_date
        original.end_date = end_date
        original.leave_type = normalized_leave_type
        original.session = normalized_session
        original.start_session = normalized_session
        original.end_session = normalized_session
        original.reason = reason

        payload_for_days = {
            "leave_type": original.leave_type,
            "start_date": original.start_date,
            "end_date": original.end_date,
            "session": original.session,
            "start_session": original.start_session,
            "end_session": original.end_session,
            "reason": original.reason,
            "status": original.status,
        }

        leave_days = compute_leave_days_for_payload(
            employee=employee, payload=payload_for_days
        )
        original.is_paid = bool(
            compute_paid_status(
                employee=employee,
                leave_type=original.leave_type,
                leave_days=leave_days,
                start_date=original.start_date,
                end_date=original.end_date,
                instance_id=original.id,
            )
        )

        with transaction.atomic():
            original.save()
            # Replace per-day rows so totals/overlap use the latest dates.
            LeaveRequestDay.objects.filter(leave_request=original).delete()
            if requested_working_dates:
                LeaveRequestDay.objects.bulk_create(
                    [
                        LeaveRequestDay(
                            leave_request=original,
                            date=d,
                            session=normalized_session,
                        )
                        for d in requested_working_dates
                    ]
                )

        try:
            from discord_app.services import update_admin_leave_message

            update_admin_leave_message(original)
        except Exception:
            logger.exception(
                "Failed updating Discord message for leave_id=%s", original.id
            )

        return Response(
            {
                "message": "Leave updated successfully",
                "action": "UPDATED",
                "id": original.id,
                "status": original.status,
                "reapplied_from": getattr(original, "reapplied_from_id", None),
            },
            status=status.HTTP_200_OK,
        )

    new_payload = {
        "leave_type": normalized_leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "session": normalized_session,
        "start_session": normalized_session,
        "end_session": normalized_session,
        "reason": reason,
        "status": "PENDING",
    }

    if hasattr(LeaveRequest, "applied_at"):
        new_payload["applied_at"] = timezone.now()

    leave_days = compute_leave_days_for_payload(employee=employee, payload=new_payload)
    new_payload["is_paid"] = bool(
        compute_paid_status(
            employee=employee,
            leave_type=normalized_leave_type,
            leave_days=leave_days,
            start_date=start_date,
            end_date=end_date,
        )
    )

    with transaction.atomic():
        new_lr = LeaveRequest.objects.create(employee=employee, **new_payload)

        if requested_working_dates:
            LeaveRequestDay.objects.bulk_create(
                [
                    LeaveRequestDay(
                        leave_request=new_lr,
                        date=d,
                        session=normalized_session,
                    )
                    for d in requested_working_dates
                ]
            )

        if hasattr(new_lr, "reapplied_from"):
            new_lr.reapplied_from = original
            new_lr.save(update_fields=["reapplied_from"])

        if hasattr(original, "has_reapplied"):
            if not original.has_reapplied:
                original.has_reapplied = True
                original.save(update_fields=["has_reapplied"])

    try:
        send_leave_request_to_admin(new_lr)
    except Exception:
        logger.exception(
            "Failed to send reapply leave request to Discord for leave_id=%s", new_lr.id
        )

    return Response(
        {
            "message": "Leave re-applied successfully",
            "action": "REAPPLIED",
            "original_leave_id": original.id,
            "id": new_lr.id,
            "status": new_lr.status,
            "reapplied_from": getattr(new_lr, "reapplied_from_id", None),
        },
        status=status.HTTP_201_CREATED,
    )
