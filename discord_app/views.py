import json
import logging
import secrets
import threading
import requests

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings

from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from form_app.models import LeaveRequest
from form_app.services import decide_leave, notify_leave_decision

from discord_app import services

logger = logging.getLogger(__name__)

DISCORD_DEBUG = settings.DISCORD_DEBUG.strip().lower() in ("1", "true", "yes", "on")


def _dbg(msg: str, *args) -> None:
    if DISCORD_DEBUG:
        logger.debug(msg, *args)


def _ephemeral(content: str, status=200):
    return JsonResponse(
        {"type": 4, "data": {"content": content, "flags": 64}}, status=status
    )


def verify_discord_signature(request, body: bytes | None = None) -> bool:
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")

    public_key = (settings.DISCORD_PUBLIC_KEY or "").strip()
    if not signature or not timestamp or not public_key:
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key))
        message_body = body if body is not None else request.body
        verify_key.verify(timestamp.encode() + message_body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def _defer_ephemeral() -> JsonResponse:
    # 5 = DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    # data.flags=64 makes it ephemeral.
    return JsonResponse({"type": 5, "data": {"flags": 64}})


def _maybe_proxy_interaction(
    *, raw_body: bytes, signature: str | None, timestamp: str | None, data: dict
) -> JsonResponse | None:
    if (
        not settings.DISCORD_INTERACTIONS_PROXY_URL
        or not settings.DISCORD_INTERACTIONS_PROXY_CHANNEL_ID
    ):
        return None

    channel_id = str(data.get("channel_id") or "").strip()
    if not channel_id or channel_id != settings.DISCORD_INTERACTIONS_PROXY_CHANNEL_ID:
        return None

    try:
        resp = requests.post(
            settings.DISCORD_INTERACTIONS_PROXY_URL,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": signature or "",
                "X-Signature-Timestamp": timestamp or "",
            },
            timeout=2.5,
        )
    except Exception:
        logger.exception(
            "Discord proxy failed url=%s channel_id=%s",
            settings.DISCORD_INTERACTIONS_PROXY_URL,
            channel_id,
        )
        return None

    # Return upstream JSON as-is.
    try:
        payload = resp.json()
        return JsonResponse(
            payload, status=resp.status_code, safe=isinstance(payload, dict)
        )
    except Exception:
        # Fallback: return a generic ephemeral error; do not block prod path.
        logger.error("Discord proxy returned non-JSON status=%s", resp.status_code)
        return _ephemeral("Proxy error", status=200)


@csrf_exempt
@require_http_methods(["POST"])
def discord_interactions(request):
    # Always verify signature before reading or parsing the body
    raw_body = request.body  # Must be the first thing you do!
    _dbg(
        "Discord signature headers present=%s public_key_configured=%s",
        bool(request.headers.get("X-Signature-Ed25519")),
        bool(settings.DISCORD_PUBLIC_KEY),
    )
    _dbg("Discord raw body prefix=%r", raw_body[:100])

    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")

    if not verify_discord_signature(request, raw_body):
        logger.warning(
            "Discord interaction rejected: invalid signature (public_key_configured=%s has_sig=%s has_ts=%s)",
            bool(settings.DISCORD_PUBLIC_KEY),
            bool(signature),
            bool(timestamp),
        )
        return JsonResponse({"error": "Invalid signature"}, status=401)

    # Only now is it safe to parse the body

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("Discord interaction rejected: invalid JSON")
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # If configured, proxy interactions for a specific channel to local/ngrok.
    proxied = _maybe_proxy_interaction(
        raw_body=raw_body, signature=signature, timestamp=timestamp, data=data
    )
    if proxied is not None:
        _dbg(
            "Discord interaction proxied channel_id=%s url=%s",
            data.get("channel_id"),
            settings.DISCORD_INTERACTIONS_PROXY_URL,
        )
        return proxied

    _dbg(
        "Discord interaction type=%s custom_id=%s",
        data.get("type"),
        (data.get("data", {}) or {}).get("custom_id"),
    )

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

        if action == "approve":
            return JsonResponse(
                {
                    "type": 9,
                    "data": {
                        "custom_id": f"leave_approve_modal_{leave_id}",
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
                        "custom_id": f"leave_reject_modal_{leave_id}",
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

        application_id = str(data.get("application_id") or "").strip()
        interaction_token = str(data.get("token") or "").strip()

        def _finish_interaction_message(msg: str) -> None:
            # Best-effort: update the deferred interaction response.
            try:
                services.edit_original_interaction_response(
                    application_id=application_id,
                    interaction_token=interaction_token,
                    content=msg,
                )
            except Exception:
                logger.exception("Failed to update interaction response")

        # APPROVE MODAL
        if parts[:3] == ["leave", "approve", "modal"]:

            def _do_approve() -> None:
                try:
                    lr = decide_leave(
                        leave_id=leave_id,
                        new_status="APPROVED",
                        message=None,
                        notify=False,
                    )
                    try:
                        notify_leave_decision(leave_id=lr.id)
                    except Exception:
                        logger.exception(
                            "notify_leave_decision failed for leave_id=%s", lr.id
                        )
                    _finish_interaction_message("Leave approved ✅")
                except LeaveRequest.DoesNotExist:
                    logger.warning(
                        "Discord approve: LeaveRequest not found (leave_id=%s). Likely env/db mismatch.",
                        leave_id,
                    )
                    _finish_interaction_message(
                        "Leave request not found. This usually means the Discord message was created by a different environment (prod vs local) than the one handling interactions."
                    )
                except ValueError as e:
                    logger.error("ValueError approving leave %s: %s", leave_id, e)
                    _finish_interaction_message(str(e))
                except Exception:
                    logger.exception("Exception approving leave %s", leave_id)
                    _finish_interaction_message("Failed to approve leave")

            # Always acknowledge within Discord's 3-second window, then finish in background.
            threading.Thread(target=_do_approve, daemon=True).start()
            return _defer_ephemeral()

        # REJECT MODAL
        if parts[:3] == ["leave", "reject", "modal"]:
            components = (data.get("data", {}) or {}).get("components", []) or []
            rejection_reason = ""

            for row in components:
                for c in row.get("components", []) or []:
                    if c.get("custom_id") == "rejection_reason":
                        rejection_reason = (c.get("value") or "").strip()

            if not rejection_reason:
                return _ephemeral("Rejection reason required")

            def _do_reject() -> None:
                try:
                    lr = decide_leave(
                        leave_id=leave_id,
                        new_status="REJECTED",
                        message=rejection_reason,
                        notify=False,
                    )
                    try:
                        notify_leave_decision(leave_id=lr.id)
                    except Exception:
                        logger.exception(
                            "notify_leave_decision failed for leave_id=%s", lr.id
                        )
                    _finish_interaction_message("Leave rejected ❌")
                except LeaveRequest.DoesNotExist:
                    logger.warning(
                        "Discord reject: LeaveRequest not found (leave_id=%s). Likely env/db mismatch.",
                        leave_id,
                    )
                    _finish_interaction_message(
                        "Leave request not found. This usually means the Discord message was created by a different environment (prod vs local) than the one handling interactions."
                    )
                except ValueError as e:
                    _finish_interaction_message(str(e))
                except Exception:
                    logger.exception("Failed to reject leave %s", leave_id)
                    _finish_interaction_message("Failed to reject leave")

            threading.Thread(target=_do_reject, daemon=True).start()
            return _defer_ephemeral()

        return _ephemeral("Unhandled modal action")

    return _ephemeral("Unhandled interaction")


# CRON AUTH HELPER


def _cron_auth_ok(request) -> bool:
    # Security note: prefer header-based token; fallback to query param for compatibility.
    token = request.headers.get("X-Cron-Token") or request.GET.get("token")
    return (
        bool(settings.DISCORD_CRON_SECRET)
        and bool(token)
        and secrets.compare_digest(token, settings.DISCORD_CRON_SECRET)
    )


# CRON: EMPLOYEE CHANNEL (ON LEAVE TODAY)


@require_http_methods(["GET"])
def cron_daily_on_leave(request):
    if not _cron_auth_ok(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    today = timezone.localdate()

    if today.weekday() in (5, 6):
        return JsonResponse(
            {"status": "skipped", "reason": "weekend", "date": str(today)}
        )

    eligible = (
        services._active_today_qs(today)
        .filter(notified_employee_at__isnull=True)
        .count()
    )
    ok = services.send_employee_on_leave_today()

    return JsonResponse(
        {
            "status": "sent" if ok else "nothing_to_send",
            "date": str(today),
            "eligible_count": eligible,
            "employee_channel_id": settings.DISCORD_EMPLOYEE_CHANNEL_ID,
        }
    )


# CRON: ADMIN CHANNEL (APPROVED TODAY)


@require_http_methods(["GET"])
def cron_daily_approved(request):
    if not _cron_auth_ok(request):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    today = timezone.localdate()

    if today.weekday() in (5, 6):
        return JsonResponse(
            {"status": "skipped", "reason": "weekend", "date": str(today)}
        )

    eligible = (
        services._approved_today_qs(today)
        .filter(notified_admin_at__isnull=True)
        .count()
    )
    ok = services.send_admin_approved_today()

    return JsonResponse(
        {
            "status": "sent" if ok else "nothing_to_send",
            "date": str(today),
            "eligible_count": eligible,
            "admin_channel_id": settings.DISCORD_ADMIN_CHANNEL_ID,
        }
    )
