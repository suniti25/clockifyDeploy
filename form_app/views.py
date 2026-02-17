from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from form_app.models import LeaveRequest
from form_app.helpers import (
    validate_leave_application_inputs,
    compute_leave_days_for_payload,
    compute_paid_status,
    display_is_paid,
)
from user_app.helpers import _get_profile_and_employee
from discord_app.services import send_leave_request_to_admin

logger = logging.getLogger(__name__)


def _parse_iso_date(value, field_name: str):
    if value is None:
        return None, Response({"error": f"{field_name} is required"}, status=status.HTTP_400_BAD_REQUEST)

    if isinstance(value, date):
        return value, None

    if isinstance(value, str):
        try:
            return date.fromisoformat(value), None
        except ValueError:
            return None, Response({"error": f"Invalid {field_name}. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

    return None, Response({"error": f"Invalid {field_name}. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_leave(request):

    profile, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    data = request.data or {}

    start_date_val = data.get("start_date")
    end_date_val = data.get("end_date")
    leave_type = data.get("leave_type")
    reason = data.get("reason", "") or ""

    # single session only
    session = data.get("session") or data.get("half_day") or "FULL"

    start_date, resp = _parse_iso_date(start_date_val, "start_date")
    if resp:
        return resp
    end_date, resp = _parse_iso_date(end_date_val, "end_date")
    if resp:
        return resp

    normalized_session, normalized_leave_type = validate_leave_application_inputs(
        profile=profile,
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        leave_type=leave_type,
        session=session,
        reason=reason,
    )

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

    leave_days = compute_leave_days_for_payload(employee=employee, payload=payload)
    payload["is_paid"] = bool(
        compute_paid_status(
            employee=employee,
            leave_type=normalized_leave_type,
            leave_days=leave_days,
            start_date=start_date,
            end_date=end_date,
        )
    )

    lr = LeaveRequest.objects.create(employee=employee, **payload)

    try:
        send_leave_request_to_admin(lr)
    except Exception:
        logger.exception("Failed to send leave request to Discord for leave_id=%s", lr.id)

    return Response(
        {
            "message": "Leave applied successfully",
            "id": lr.id,
            "leave_type": lr.leave_type,
            "start_date": lr.start_date.isoformat(),
            "end_date": lr.end_date.isoformat(),
            "session": lr.session,
            "status": lr.status,
            "is_paid": display_is_paid(lr.leave_type, getattr(lr, "is_paid", None)),
            "days": float(lr.total_days()),
            "reapplied_from": getattr(lr, "reapplied_from_id", None),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_requests(request):
    """
    GET /api/form/requests/
    Returns current user's leave requests (latest first)
    """
    _, employee, err = _get_profile_and_employee(request)
    if err:
        return Response([], status=status.HTTP_200_OK)

    qs = LeaveRequest.objects.filter(employee=employee).order_by("-id")

    results = []
    for lr in qs[:200]:
        created_at = getattr(lr, "applied_at", None) or getattr(lr, "created_at", None)
        results.append(
            {
                "id": lr.id,
                "leave_type": lr.leave_type,
                "start_date": lr.start_date.isoformat() if lr.start_date else None,
                "end_date": lr.end_date.isoformat() if lr.end_date else None,
                "session": getattr(lr, "session", "FULL"),
                "status": lr.status,
                "is_paid": display_is_paid(lr.leave_type, getattr(lr, "is_paid", None)),
                "days": float(lr.total_days()) if hasattr(lr, "total_days") else None,
                "reason": getattr(lr, "reason", "") or "",
                "submitted_at": created_at.isoformat() if created_at else None,
                "discord_message_id": getattr(lr, "discord_message_id", None),
                "reapplied_from": getattr(lr, "reapplied_from_id", None),
            }
        )

    return Response(results, status=status.HTTP_200_OK)

@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def update_leave_by_body(request):
    profile, employee, err = _get_profile_and_employee(request)
    if err:
        return err

    data = request.data or {}

    leave_id = data.get("id") or data.get("form_id")
    if not leave_id:
        return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)

    form_payload = data.get("form_payload")
    payload = form_payload if isinstance(form_payload, dict) else data

    original = get_object_or_404(LeaveRequest, id=leave_id, employee=employee)
    if original.status == LeaveRequest.STATUS_VOIDED:
        return Response(
            {"error": "Voided leave requests cannot be updated"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    start_date_val = payload.get("start_date", original.start_date)
    end_date_val = payload.get("end_date", original.end_date)
    leave_type = payload.get("leave_type", original.leave_type)
    reason = payload.get("reason", getattr(original, "reason", "")) or ""
    session = payload.get("session") or payload.get("half_day") or original.session or "FULL"

    start_date, resp = _parse_iso_date(start_date_val, "start_date")
    if resp:
        return resp
    end_date, resp = _parse_iso_date(end_date_val, "end_date")
    if resp:
        return resp

    normalized_session, normalized_leave_type = validate_leave_application_inputs(
        profile=profile,
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        leave_type=leave_type,
        session=session,
        reason=reason,
        instance_id=original.id,
    )

    # CASE 1: PENDING -> UPDATE
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

        leave_days = compute_leave_days_for_payload(employee=employee, payload=payload_for_days)
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

        original.save()

        try:
            from discord_app.services import update_admin_leave_message
            update_admin_leave_message(original)
        except Exception:
            logger.exception("Failed updating Discord message for leave_id=%s", original.id)

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


    # CASE 2: APPROVED/REJECTED -> REAPPLY (create new)

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

        # link reapply history
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
        logger.exception("Failed to send reapply leave request to Discord for leave_id=%s", new_lr.id)

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

