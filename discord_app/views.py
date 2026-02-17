import json
import os
import logging
import secrets
import threading

from dotenv import load_dotenv
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.utils import timezone

from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from form_app.models import LeaveRequest
from form_app.services import decide_leave, notify_leave_decision

from discord_app import services

load_dotenv()
logger = logging.getLogger(__name__)

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")
DISCORD_CRON_SECRET = os.getenv("DISCORD_CRON_SECRET", "")
DISCORD_DEBUG = os.getenv("DISCORD_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _dbg(msg: str, *args) -> None:
    if DISCORD_DEBUG:
        logger.debug(msg, *args)


def _ephemeral(content: str, status=200):
    return JsonResponse({"type": 4, "data": {"content": content, "flags": 64}}, status=status)


def verify_discord_signature(request, body: bytes | None = None) -> bool:
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
    # Always verify signature before reading or parsing the body
    raw_body = request.body  # Must be the first thing you do!
    _dbg("Discord signature headers present=%s", bool(request.headers.get("X-Signature-Ed25519")))
    _dbg("Discord raw body prefix=%r", raw_body[:100])

    if not verify_discord_signature(request, raw_body):
        logger.warning("Discord interaction rejected: invalid signature")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    # Only now is it safe to parse the body

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("Discord interaction rejected: invalid JSON")
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    _dbg("Discord interaction type=%s custom_id=%s", data.get("type"), (data.get("data", {}) or {}).get("custom_id"))

    interaction_type = data.get("type")

    # 1 = PING
    if interaction_type == 1:
        return JsonResponse({"type": 1})

    # 3 = BUTTON CLICK
    if interaction_type == 3:
        custom_id = (data.get("data", {}) or {}).get("custom_id", "") or ""
        parts = custom_id.split("_")

        if len(parts) < 3 or parts[0] != "leave":
            return _ephemeral("Invalid button action")

        action = parts[1]
        try:
            leave_id = int(parts[-1])
        except ValueError:
            return _ephemeral("Invalid leave ID")

        leave_request = get_object_or_404(
            LeaveRequest.objects.select_related("employee", "employee__user"),
            id=leave_id,
        )

        if leave_request.status != "PENDING":
            return _ephemeral("This request is already processed.")

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
                                        "min_length": 5,
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    },
                }
            )

        return _ephemeral("Unknown action")

    # 5 = MODAL SUBMISSION
    if interaction_type == 5:
        custom_id = (data.get("data", {}) or {}).get("custom_id", "") or ""
        parts = custom_id.split("_")

        if len(parts) < 4 or parts[0] != "leave":
            return _ephemeral("Invalid modal action")

        try:
            leave_id = int(parts[-1])
        except ValueError:
            return _ephemeral("Invalid leave ID")

        # APPROVE MODAL
        if parts[:3] == ["leave", "approve", "modal"]:
            try:
                # Do the minimal DB update in the request/response cycle.
                # Defer Discord updates / email / other side effects to avoid interaction timeouts.
                lr = decide_leave(
                    leave_id=leave_id,
                    new_status="APPROVED",
                    message=None,
                    notify=False,
                )

                threading.Thread(
                    target=notify_leave_decision,
                    kwargs={"leave_id": lr.id},
                    daemon=True,
                ).start()
            except ValueError as e:
                logger.error(f"ValueError approving leave {leave_id}: {e}")
                return _ephemeral(str(e))
            except Exception as ex:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"Exception approving leave {leave_id}: {ex}\n{tb}")
                return _ephemeral(f"Failed to approve leave: {ex}")

            return _ephemeral("Leave approved ✅")

        # REJECT MODAL
        if parts[:3] == ["leave", "reject", "modal"]:
            components = (data.get("data", {}) or {}).get("components", []) or []
            rejection_reason = ""

            for row in components:
                for c in (row.get("components", []) or []):
                    if c.get("custom_id") == "rejection_reason":
                        rejection_reason = (c.get("value") or "").strip()

            if not rejection_reason:
                return _ephemeral("Rejection reason required")

            try:
                lr = decide_leave(
                    leave_id=leave_id,
                    new_status="REJECTED",
                    message=rejection_reason,
                    notify=False,
                )

                threading.Thread(
                    target=notify_leave_decision,
                    kwargs={"leave_id": lr.id},
                    daemon=True,
                ).start()
            except ValueError as e:
                return _ephemeral(str(e))
            except Exception as ex:
                import traceback
                tb = traceback.format_exc()
                logger.exception("Failed to reject leave %s", leave_id)
                return _ephemeral("Failed to reject leave")

            return _ephemeral("Leave rejected ❌")

        return _ephemeral("Unhandled modal action")

    return _ephemeral("Unhandled interaction")

# CRON AUTH HELPER

def _cron_auth_ok(request) -> bool:
    # Security note: prefer header-based token; fallback to query param for compatibility.
    token = request.headers.get("X-Cron-Token") or request.GET.get("token")
    return bool(DISCORD_CRON_SECRET) and bool(token) and secrets.compare_digest(token, DISCORD_CRON_SECRET)

# CRON: EMPLOYEE CHANNEL (ON LEAVE TODAY)

@require_http_methods(["GET"])
def cron_daily_on_leave(request):
    if not _cron_auth_ok(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    today = timezone.localdate()

    if today.weekday() in (5, 6):
        return JsonResponse({"status": "skipped", "reason": "weekend", "date": str(today)})

    eligible = services._active_today_qs(today).filter(notified_employee_at__isnull=True).count()
    ok = services.send_employee_on_leave_today()

    return JsonResponse(
        {
            "status": "sent" if ok else "nothing_to_send",
            "date": str(today),
            "eligible_count": eligible,
            "employee_channel_id": services.EMPLOYEE_CHANNEL_ID,
        }
    )

# CRON: ADMIN CHANNEL (APPROVED TODAY)

@require_http_methods(["GET"])
def cron_daily_approved(request):
    if not _cron_auth_ok(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    today = timezone.localdate()

    if today.weekday() in (5, 6):
        return JsonResponse({"status": "skipped", "reason": "weekend", "date": str(today)})

    eligible = services._approved_today_qs(today).filter(notified_admin_at__isnull=True).count()
    ok = services.send_admin_approved_today()

    return JsonResponse(
        {
            "status": "sent" if ok else "nothing_to_send",
            "date": str(today),
            "eligible_count": eligible,
            "admin_channel_id": services.ADMIN_CHANNEL_ID,
        }
    )
