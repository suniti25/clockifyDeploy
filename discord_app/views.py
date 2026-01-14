from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
import os
from dotenv import load_dotenv
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from datetime import datetime
import pytz

from form_app.models import LeaveRequest
from discord_app.services import send_rejection_email_to_employee

load_dotenv()

DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')


def verify_discord_signature(request, body=None):
    """Verify Discord interaction signature using Ed25519."""
    signature = request.headers.get('X-Signature-Ed25519')
    timestamp = request.headers.get('X-Signature-Timestamp')

    if not signature or not timestamp or not DISCORD_PUBLIC_KEY:
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        message_body = body if body is not None else request.body
        message = timestamp.encode() + message_body
        verify_key.verify(message, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False

@csrf_exempt
@require_http_methods(['POST'])
def discord_interactions(request):
    """Handle Discord interactions (button clicks and modal submissions)."""
    raw_body = request.body
    
    if not verify_discord_signature(request, raw_body):
        return JsonResponse({"error": "Invalid signature"}, status=401)
    
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    interaction_type = data.get('type')

    if interaction_type == 1:
        return JsonResponse({"type": 1})

    if interaction_type == 3:
        custom_id = data.get('data', {}).get('custom_id', '')
        parts = custom_id.split('_')
        
        if len(parts) < 3 or parts[0] != 'leave':
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "❌ Invalid button action",
                    "flags": 64
                }
            }, status=200)

        leave_id = None
        action = None
        
        if len(parts) == 3:
            action = parts[1]
            try:
                leave_id = int(parts[2])
            except (ValueError, IndexError):
                pass
        elif len(parts) == 4:
            action = parts[1]
            try:
                leave_id = int(parts[3])
            except (ValueError, IndexError):
                pass
        else:
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "❌ Invalid button format",
                    "flags": 64
                }
            }, status=200)

        if leave_id is None:
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "❌ Invalid leave request ID",
                    "flags": 64
                }
            }, status=200)

        try:
            leave_request = LeaveRequest.objects.get(id=leave_id)
        except LeaveRequest.DoesNotExist:
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "❌ Leave request not found",
                    "flags": 64
                }
            }, status=200)

        if leave_request.status != 'PENDING':
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "This request is already processed.",
                    "flags": 64
                }
            }, status=200)

        if action == 'approve':   
            return JsonResponse({
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
                                    "min_length": 0,
                                    "max_length": 1,
                                    "placeholder": "No input needed",
                                    "required": False
                                }
                            ]
                        }
                    ]
                }
            }, status=200)

        elif action == 'reject':
            return JsonResponse({
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
                                    "placeholder": "Enter the reason for rejecting this leave request",
                                    "min_length": 10,
                                    "max_length": 500,
                                    "required": True
                                }
                            ]
                        }
                    ]
                }
            }, status=200)

        elif action == 'cancel':
            # Rebuild the original leave request embed
            paid_status = "Paid" if leave_request.is_paid else "Unpaid"
            leave_type_display = leave_request.get_leave_type_display()
            nepal_tz = pytz.timezone('Asia/Kathmandu')
            applied_datetime_nepal = leave_request.applied_at.astimezone(nepal_tz)
            applied_datetime = applied_datetime_nepal.strftime('%d %B %Y at %I:%M %p')
            
            embed = {
                "title": f"{leave_request.employee.user.get_full_name()} - {paid_status} {leave_type_display} Leave Request",
                "color": 3447003, 
                "fields": [
                    {
                        "name": "Employee Name",
                        "value": leave_request.employee.user.get_full_name(),
                        "inline": False
                    },
                    {
                        "name": "Duration",
                        "value": f"{leave_request.start_date.strftime('%d %B %Y')} to {leave_request.end_date.strftime('%d %B %Y')}",
                        "inline": False
                    },
                    {
                        "name": "Leave Type",
                        "value": leave_type_display,
                        "inline": True
                    },
                    {
                        "name": "Session",
                        "value": leave_request.get_session_display(),
                        "inline": True
                    },
                    {
                        "name": "Total Day/s",
                        "value": f"{leave_request.total_days()} day(s)",
                        "inline": True
                    },
                    {
                        "name": "Reason",
                        "value": leave_request.reason or "No reason provided",
                        "inline": False
                    },
                    {
                        "name": "Date and time of request",
                        "value": applied_datetime,
                        "inline": False
                    }
                ],
                "timestamp": datetime.now().isoformat()
            }
            
            return JsonResponse({
                "type": 7,
                "data": {
                    "embeds": [embed],
                    "components": [
                        {
                            "type": 1,
                            "components": [
                                {"type": 2, "style": 3, "label": "Approve", "custom_id": f"leave_approve_{leave_request.id}"},
                                {"type": 2, "style": 4, "label": "Reject", "custom_id": f"leave_reject_{leave_request.id}"}
                            ]
                        }
                    ]
                }
            }, status=200)

        else:
            return JsonResponse({
                "type": 4,
                "data": {"content": "❌ Unknown action", "flags": 64}
            }, status=200)

    if interaction_type == 5:
        custom_id = data.get('data', {}).get('custom_id', '')
        parts = custom_id.split('_')
        
        # Handle approve modal submission
        if len(parts) == 4 and parts[0] == 'leave' and parts[1] == 'approve' and parts[2] == 'modal':
            try:
                leave_id = int(parts[3])
            except (ValueError, IndexError):
                return JsonResponse({
                    "type": 4,
                    "data": {
                        "content": "❌ Invalid leave request ID",
                        "flags": 64
                    }
                }, status=200)

            try:
                leave_request = LeaveRequest.objects.get(id=leave_id)
            except LeaveRequest.DoesNotExist:
                return JsonResponse({
                    "type": 4,
                    "data": {
                        "content": "❌ Leave request not found",
                        "flags": 64
                    }
                }, status=200)

            # No need to validate input - just submitting the modal is confirmation enough
            leave_request.status = 'APPROVED'
            leave_request.save(update_fields=['status'])
            
            response_message = f"✅ **APPROVED**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}\n\n📢 Posted to leaves_and_notices channel"
            
            return JsonResponse({
                "type": 7,
                "data": {
                    "content": response_message,
                    "components": []
                }
            }, status=200)
        
        # Handle reject modal submission
        elif len(parts) == 4 and parts[0] == 'leave' and parts[1] == 'reject' and parts[2] == 'modal':
            try:
                leave_id = int(parts[3])
            except (ValueError, IndexError):
                return JsonResponse({
                    "type": 4,
                    "data": {
                        "content": "❌ Invalid leave request ID",
                        "flags": 64
                    }
                }, status=200)

            try:
                leave_request = LeaveRequest.objects.get(id=leave_id)
            except LeaveRequest.DoesNotExist:
                return JsonResponse({
                    "type": 4,
                    "data": {
                        "content": "❌ Leave request not found",
                        "flags": 64
                    }
                }, status=200)

            components = data.get('data', {}).get('components', [])
            rejection_reason = ""
            
            for action_row in components:
                for component in action_row.get('components', []):
                    if component.get('custom_id') == 'rejection_reason':
                        rejection_reason = component.get('value', '')
                        break
            
            if not rejection_reason:
                return JsonResponse({
                    "type": 4,
                    "data": {
                        "content": "❌ Rejection reason is required",
                        "flags": 64
                    }
                }, status=200)

            leave_request.status = 'REJECTED'
            leave_request.rejection_reason = rejection_reason
            leave_request.save(update_fields=['status', 'rejection_reason'])
            send_rejection_email_to_employee(leave_request)
            response_message = f"❌ **REJECTED**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}\n\n**Reason:** {rejection_reason}\n\n📧 Rejection email sent to employee"
            
            return JsonResponse({
                "type": 7,
                "data": {
                    "content": response_message,
                    "components": []
                }
            }, status=200)
        else:
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": "❌ Invalid modal submission",
                    "flags": 64
                }
            }, status=200)

    return JsonResponse({
        "type": 4,
        "data": {"content": "❌ Unknown interaction type", "flags": 64}
    }, status=200)