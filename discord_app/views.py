from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
from form_app.models import LeaveRequest
import os
from dotenv import load_dotenv
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from discord_app.services import send_approved_leave_to_employees

load_dotenv()

DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')


def verify_discord_signature(request, body=None):
    """Verify Discord interaction signature using Ed25519 (required by Discord)."""
    signature = request.headers.get('X-Signature-Ed25519')
    timestamp = request.headers.get('X-Signature-Timestamp')

    if not signature or not timestamp or not DISCORD_PUBLIC_KEY:
        print(f"Missing signature components: sig={bool(signature)}, ts={bool(timestamp)}, key={bool(DISCORD_PUBLIC_KEY)}")
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        message_body = body if body is not None else request.body
        message = timestamp.encode() + message_body
        verify_key.verify(message, bytes.fromhex(signature))
        print("Signature verification SUCCESS")
        return True
    except (BadSignatureError, ValueError) as e:
        print(f"Discord signature verification failed: {e}")
        return False


@csrf_exempt
@require_http_methods(['POST'])
def discord_interactions(request):
    """Handle Discord interactions (button clicks)"""
    
    raw_body = request.body
    
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    interaction_type = data.get('type')
    print(f"Discord interaction type: {interaction_type}")

    # PING from Discord
    if interaction_type == 1:
        print("Got PING from Discord, responding with type 1")
        return JsonResponse({"type": 1})

    # Verify signature for all other interactions
    if not verify_discord_signature(request, raw_body):
        print("Signature verification FAILED")
        return JsonResponse({"error": "Invalid signature"}, status=401)

    # MESSAGE_COMPONENT (button click)
    if interaction_type == 3:
        custom_id = data.get('data', {}).get('custom_id', '')
        print(f"Got button click! custom_id: {custom_id}")

        # Parse custom_id: leave_<action>_<id>
        parts = custom_id.split('_')
        if len(parts) != 3 or parts[0] != 'leave':
            print(f"Invalid custom_id format: {custom_id}")
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": " Invalid button action",
                    "flags": 64
                }
            }, status=200)

        action = parts[1]  # approve | reject | confirm
        try:
            leave_id = int(parts[2])
            print(f"Processing action '{action}' for leave_id {leave_id}")
        except (ValueError, IndexError) as e:
            print(f"Error parsing leave_id: {e}")
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": " Invalid leave request ID",
                    "flags": 64
                }
            }, status=200)

        try:
            leave_request = LeaveRequest.objects.get(id=leave_id)
            print(f"Found leave request: {leave_request.id} - {leave_request.employee.name}")
        except LeaveRequest.DoesNotExist:
            print(f"Leave request {leave_id} not found")
            return JsonResponse({
                "type": 4,
                "data": {
                    "content": " Leave request not found",
                    "flags": 64
                }
            }, status=200)

        # Handle different actions
        if action == 'approve':
            # Just update message, don't save to database yet
            response_message = f" **Approved** (Pending Confirmation): {leave_request.employee.name} - {leave_request.get_leave_type_display()}\n\n**Click 'Confirm' to finalize**"
            
            # Update the message with only Confirm and Reject buttons
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
                                    "label": "Confirm Approval",
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
            # Just update message, don't save to database yet
            response_message = f" **Rejected** (Pending Confirmation): {leave_request.employee.name} - {leave_request.get_leave_type_display()}\n\n**Click 'Confirm' to finalize**"
            
            # Update the message with only Confirm button
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
                                    "style": 4,
                                    "label": "Confirm Rejection",
                                    "custom_id": f"leave_confirm_reject_{leave_request.id}"
                                },
                                {
                                    "type": 2,
                                    "style": 2,
                                    "label": "Cancel",
                                    "custom_id": f"leave_cancel_{leave_request.id}"
                                }
                            ]
                        }
                    ]
                }
            }, status=200)

        elif action == 'confirm':
            # This is the final confirmation
            # Check if it's approve or reject
            if len(parts) == 4:  # leave_confirm_approve/reject_<id>
                final_action = parts[2]  # approve or reject
                
                if final_action == 'approve':
                    leave_request.status = 'APPROVED'
                    leave_request.save(update_fields=['status'])
                    
                    # Send to leaves_and_notices channel
                    send_approved_leave_to_employees(leave_request)
                    
                    response_message = f"**CONFIRMED & APPROVED**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}\n\n📢 Posted to leaves_and_notices channel"
                    print(f"Leave request {leave_id} approved and posted")
                    
                elif final_action == 'reject':
                    leave_request.status = 'REJECTED'
                    leave_request.save(update_fields=['status'])
                    
                    response_message = f" **CONFIRMED & REJECTED**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}"
                    print(f"Leave request {leave_id} rejected")
                else:
                    return JsonResponse({
                        "type": 4,
                        "data": {"content": " Invalid confirmation action", "flags": 64}
                    }, status=200)
                
                # Update message and remove buttons
                return JsonResponse({
                    "type": 7,  # UPDATE_MESSAGE
                    "data": {
                        "content": response_message,
                        "components": []  # Remove all buttons
                    }
                }, status=200)
            else:
                return JsonResponse({
                    "type": 4,
                    "data": {"content": " Invalid confirmation format", "flags": 64}
                }, status=200)

        elif action == 'cancel':
            # Reset to original state
            from datetime import datetime
            from discord_app.services import send_discord_message
            
            response_message = f"🔄 **Action Cancelled**: {leave_request.employee.name} - {leave_request.get_leave_type_display()}"
            
            # Restore original buttons
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
            print(f"Unknown action: {action}")
            return JsonResponse({
                "type": 4,
                "data": {"content": "❌ Unknown action", "flags": 64}
            }, status=200)

    # Unknown interaction type
    print(f"Unhandled interaction type: {interaction_type}")
    return JsonResponse({
        "type": 4,
        "data": {"content": "❌ Unknown interaction type", "flags": 64}
    }, status=200)