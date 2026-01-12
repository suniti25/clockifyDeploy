from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
import os
from dotenv import load_dotenv
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

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
            response_message = f"✅ **Confirm Approval**\n\nAre you sure you want to approve this leave request?\n\n**Employee:** {leave_request.employee.name}\n**Leave Type:** {leave_request.get_leave_type_display()}\n**Start Date:** {leave_request.start_date.strftime('%d %B %Y')}\n**End Date:** {leave_request.end_date.strftime('%d %B %Y')}"
            
            return JsonResponse({
                "type": 7,  # UPDATE_MESSAGE
                "data": {
                    "content": response_message,
                    "components": [
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 2,
                                    "style": 3,
                                    "label": "Yes, Approve",
                                    "custom_id": f"leave_confirm_approve_{leave_request.id}"
                                },
                                {
                                    "type": 2,
                                    "style": 4,
                                    "label": "Cancel",
                                    "custom_id": f"leave_cancel_{leave_request.id}"
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

        elif action == 'confirm':
            if len(parts) == 4:
                final_action = parts[2]
                
                if final_action == 'approve':
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
                else:
                    return JsonResponse({
                        "type": 4,
                        "data": {"content": "❌ Invalid confirmation action", "flags": 64}
                    }, status=200)
            else:
                return JsonResponse({
                    "type": 4,
                    "data": {"content": "❌ Invalid confirmation format", "flags": 64}
                }, status=200)

        elif action == 'cancel':
            response_message = f"🔄 **Action Cancelled**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}"
            
            return JsonResponse({
                "type": 7,
                "data": {
                    "content": response_message,
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
        
        if len(parts) == 4 and parts[0] == 'leave' and parts[1] == 'reject' and parts[2] == 'modal':
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