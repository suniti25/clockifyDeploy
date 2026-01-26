# form_app/views.py

import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from user_app.models import Profile
from .models import LeaveRequest
from .serializers import LeaveCreateSerializer, LeaveResponseSerializer

logger = logging.getLogger(__name__)


def _get_employee_or_response(request):
    """
    Shared guard:
    - profile must exist
    - admins are blocked for employee actions
    - employee must exist
    Returns: (employee, None) OR (None, Response)
    """
    try:
        profile = Profile.objects.select_related("employee").get(user=request.user)
    except Profile.DoesNotExist:
        return None, Response({"error": "Profile not found"}, status=status.HTTP_403_FORBIDDEN)

    if profile.role == "ADMIN":
        return None, Response({"error": "Admins cannot perform this action"}, status=status.HTTP_403_FORBIDDEN)

    employee = profile.employee
    if not employee:
        return None, Response({"error": "Employee record not found"}, status=status.HTTP_404_NOT_FOUND)

    return employee, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_leave(request):
    """
    Endpoint to apply for a new leave request.
    """
    employee, err = _get_employee_or_response(request)
    if err:
        return err

    serializer = LeaveCreateSerializer(data=request.data, context={"request": request})

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    leave = serializer.save()
    response_data = LeaveResponseSerializer(leave).data

    # Include warning if serializer set one (optional pattern)
    warning = serializer.context.get("warning") if isinstance(serializer.context, dict) else None
    if warning:
        response_data["warning"] = warning

    # Send Discord notification (best-effort)
    try:
        from discord_app.services import send_leave_request_to_admin
        send_leave_request_to_admin(leave)
    except ImportError:
        logger.warning("discord_app.services not available (ImportError). Skipping Discord notification.")
    except Exception:
        logger.exception("Failed to send leave request to Discord admin channel")

    return Response(
        {"message": "Leave request submitted successfully", "data": response_data},
        status=status.HTTP_201_CREATED,
    )


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def update_leave(request, pk):
    """
    Update an existing leave request (only own leaves, only if PENDING).
    """
    employee, err = _get_employee_or_response(request)
    if err:
        return err

    leave = get_object_or_404(LeaveRequest, id=pk, employee=employee)

    if leave.status != "PENDING":
        return Response(
            {"error": "Only pending leave requests can be updated."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = LeaveCreateSerializer(
        instance=leave,
        data=request.data,
        context={"request": request},
        partial=True,
    )

    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    updated_leave = serializer.save()

    # Update Discord message (best-effort)
    if updated_leave.discord_message_id:
        logger.info(
            "Updating Discord message %s for leave %s",
            updated_leave.discord_message_id,
            updated_leave.id,
        )
        try:
            from discord_app.services import update_admin_leave_message
            result = update_admin_leave_message(updated_leave)
            logger.info("Discord update result: %s", result)
        except Exception:
            logger.exception("Discord update failed for leave %s", updated_leave.id)
    else:
        logger.warning("No discord_message_id found for leave %s", updated_leave.id)

    return Response(
        {"message": "Leave request updated successfully", "data": LeaveResponseSerializer(updated_leave).data},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_requests(request):
    """
    List all leave requests for the logged-in employee (newest first).
    """
    employee, err = _get_employee_or_response(request)
    if err:
        return err

    leave_requests = LeaveRequest.objects.filter(employee=employee).order_by("-applied_at")

    results = []
    for req in leave_requests:
        # Format date range for frontend
        if req.start_date == req.end_date:
            date_range = req.start_date.strftime("%b %d, %Y")
        else:
            date_range = f"{req.start_date.strftime('%b %d, %Y')} - {req.end_date.strftime('%b %d, %Y')}"

        results.append(
            {
                "id": req.id,
                "type": req.get_leave_type_display(),
                "status": req.status.capitalize(),
                "dateRange": date_range,
                "days": req.total_days(),
                "reason": req.reason or "-",
                "requested": req.applied_at.strftime("%b %d, %Y") if req.applied_at else "-",
            }
        )

    return Response(results, status=status.HTTP_200_OK)
