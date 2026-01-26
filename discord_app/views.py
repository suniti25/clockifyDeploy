import json
import os
import logging
from dotenv import load_dotenv
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404

from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from form_app.models import LeaveRequest
from discord_app.services import (
    send_rejection_email_to_employee,
    update_discord_leave_message,
)

load_dotenv()
logger = logging.getLogger(__name__)

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")

# VERIFY DISCORD SIGNATURE
def verify_discord_signature(request, body=None):
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")

    if not signature or not timestamp or not DISCORD_PUBLIC_KEY:
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        message_body = body if body is not None else request.body
        verify_key.verify(timestamp.encode() + message_body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


@csrf_exempt
@require_http_methods(["POST"])
def discord_interactions(request):
    raw_body = request.body

    if not verify_discord_signature(request, raw_body):
        return JsonResponse({"error": "Invalid signature"}, status=401)

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    interaction_type = data.get("type")

    # 1 = PING
    if interaction_type == 1:
        return JsonResponse({"type": 1})

    # 3 = BUTTON CLICK
    if interaction_type == 3:
        custom_id = data.get("data", {}).get("custom_id", "")
        parts = custom_id.split("_")

        # expected: leave_approve_<id> OR leave_reject_<id>
        if len(parts) < 3 or parts[0] != "leave":
            return JsonResponse(
                {"type": 4, "data": {"content": "Invalid button action", "flags": 64}}
            )

        action = parts[1]
        try:
            leave_id = int(parts[-1])
        except ValueError:
            return JsonResponse(
                {"type": 4, "data": {"content": "Invalid leave ID", "flags": 64}}
            )

        leave_request = get_object_or_404(LeaveRequest, id=leave_id)

        if leave_request.status != "PENDING":
            return JsonResponse(
                {
                    "type": 4,
                    "data": {"content": "This request is already processed.", "flags": 64},
                }
            )

        if action == "approve":
            return JsonResponse(
                {
                    "type": 9,
                    "data": {
                        "custom_id": f"leave_approve_modal_{leave_request.id}",
                        "title": "Confirm Leave Approval",
                        "components": [
                            {
                                "type": 1,
                                "components": [
                                    {
                                        "type": 4,
                                        "custom_id": "confirm_approval",
                                        "label": "Press Submit to confirm approval",
                                        "style": 1,
                                        "required": False,
                                    }
                                ],
                            }
                        ],
                    },
                }
            )

        if action == "reject":
            return JsonResponse(
                {
                    "type": 9,
                    "data": {
                        "custom_id": f"leave_reject_modal_{leave_request.id}",
                        "title": "Reject Leave Request",
                        "components": [
                            {
                                "type": 1,
                                "components": [
                                    {
                                        "type": 4,
                                        "custom_id": "rejection_reason",
                                        "label": "Reason for Rejection",
                                        "style": 2,
                                        "min_length": 10,
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            )

        return JsonResponse({"type": 4, "data": {"content": "Unknown action", "flags": 64}})

    #  MODAL SUBMISSION
    if interaction_type == 5:
        custom_id = data.get("data", {}).get("custom_id", "")
        parts = custom_id.split("_")

        # expected: leave_approve_modal_<id> OR leave_reject_modal_<id>
        if len(parts) < 4 or parts[0] != "leave":
            return JsonResponse(
                {"type": 4, "data": {"content": "Invalid modal action", "flags": 64}}
            )

        try:
            leave_id = int(parts[-1])
        except ValueError:
            return JsonResponse(
                {"type": 4, "data": {"content": "Invalid leave ID", "flags": 64}}
            )

        leave_request = get_object_or_404(LeaveRequest, id=leave_id)

        # Prevent double processing
        if leave_request.status != "PENDING":
            return JsonResponse(
                {
                    "type": 4,
                    "data": {"content": "This request is already processed.", "flags": 64},
                }
            )

        # APPROVE MODAL (REPLACED AS YOU REQUESTED)
        if parts[:3] == ["leave", "approve", "modal"]:
            leave_request.status = "APPROVED"
            leave_request.save(update_fields=["status"])

            try:
                update_discord_leave_message(leave_request)
            except Exception:
                logger.exception("Failed to update Discord message after approval")

            return JsonResponse(
                {"type": 7, "data": {"content": "Leave approved", "components": []}}
            )

        # REJECT MODAL
        if parts[:3] == ["leave", "reject", "modal"]:
            components = data.get("data", {}).get("components", [])
            rejection_reason = ""

            for row in components:
                for c in row.get("components", []):
                    if c.get("custom_id") == "rejection_reason":
                        rejection_reason = c.get("value", "").strip()

            if not rejection_reason:
                return JsonResponse(
                    {"type": 4, "data": {"content": "Rejection reason required", "flags": 64}}
                )

            leave_request.status = "REJECTED"
            leave_request.rejection_reason = rejection_reason
            leave_request.save(update_fields=["status", "rejection_reason"])

            # Update Discord + send email notification
            try:
                update_discord_leave_message(leave_request)
            except Exception:
                logger.exception("Failed to update Discord message after rejection")

            send_rejection_email_to_employee(leave_request)

            return JsonResponse(
                {"type": 7, "data": {"content": "Leave rejected", "components": []}}
            )

        return JsonResponse(
            {"type": 4, "data": {"content": "Unhandled modal action", "flags": 64}}
        )

    return JsonResponse(
        {"type": 4, "data": {"content": "Unhandled interaction", "flags": 64}}
    )
