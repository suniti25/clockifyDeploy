import aiohttp
import asyncio
import os
from datetime import datetime
import pytz
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')

# Channel IDs - with safe defaults
ADMIN_CHANNEL_ID: int = 0
EMPLOYEE_CHANNEL_ID: int = 0

try:
    admin_id = os.getenv('DISCORD_ADMIN_CHANNEL_ID', '0')
    ADMIN_CHANNEL_ID = int(admin_id) if admin_id else 0
except (ValueError, TypeError):
    ADMIN_CHANNEL_ID = 0

try:
    employee_id = os.getenv('DISCORD_EMPLOYEE_CHANNEL_ID', '0')
    EMPLOYEE_CHANNEL_ID = int(employee_id) if employee_id else 0
except (ValueError, TypeError):
    EMPLOYEE_CHANNEL_ID = 0

# Optional name-based lookup
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID', '')
ADMIN_CHANNEL_NAME = os.getenv('DISCORD_ADMIN_CHANNEL_NAME', '')
EMPLOYEE_CHANNEL_NAME = os.getenv('DISCORD_EMPLOYEE_CHANNEL_NAME', '')
# Channel used for daily leave summaries; defaults to the admin channel name "leaves_and_notices"
SUMMARY_CHANNEL_NAME = os.getenv('DISCORD_LEAVE_SUMMARY_CHANNEL_NAME', 'leaves_and_notices')
_channel_cache = {}


async def resolve_channel_id_by_name(session, channel_name):
    """Resolve channel ID by name within a guild, with simple cache."""
    if not DISCORD_GUILD_ID or not channel_name:
        return None
    cache_key = f"{DISCORD_GUILD_ID}:{channel_name.lower()}"
    if cache_key in _channel_cache:
        return _channel_cache[cache_key]

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
    }
    async with session.get(url, headers=headers) as response:
        if response.status != 200:
            body = await response.text()
            print(f"Discord API Error (list channels): {response.status} | {body}")
            return None
        channels = await response.json()
        for ch in channels:
            if ch.get("name", "").lower() == channel_name.lower():
                _channel_cache[cache_key] = ch.get("id")
                return ch.get("id")
    print(f"Channel name '{channel_name}' not found in guild {DISCORD_GUILD_ID}")
    return None


async def send_discord_message(channel_id=None, embed_dict=None, content=None, channel_name=None, components=None):
    """Send message to Discord channel via API. Supports ID or name lookup with one retry on 404."""
    if not DISCORD_TOKEN:
        print("Warning: DISCORD_TOKEN not configured")
        return False

    async with aiohttp.ClientSession() as session:
        # Resolve channel ID if not provided and name lookup is requested
        if (not channel_id or channel_id == 0) and channel_name:
            channel_id = await resolve_channel_id_by_name(session, channel_name)

        if not channel_id or channel_id == 0:
            print("Warning: channel_id not configured and channel_name lookup failed")
            return False

        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json"
        }

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

        data = {}
        if content:
            data["content"] = content
        if embed_dict:
            data["embeds"] = [embed_dict]
        if components:
            data["components"] = components

        async def _post(to_channel_id):
            post_url = f"https://discord.com/api/v10/channels/{to_channel_id}/messages"
            async with session.post(post_url, json=data, headers=headers) as response:
                return response.status, await response.text()

        try:
            status, body = await _post(channel_id)
            if status in (200, 201, 204):
                return True
            
            if status == 404 and channel_name:
                resolved = await resolve_channel_id_by_name(session, channel_name)
                if resolved and resolved != channel_id:
                    retry_status, retry_body = await _post(resolved)
                    if retry_status in (200, 201, 204):
                        print(f"Discord send succeeded after resolving channel name '{channel_name}' to {resolved}")
                        return True
                    print(f"Discord retry failed ({retry_status}): {retry_body}")
                    return False

            print(f"Discord API Error: {status} | {body}")
            return False
        except Exception as e:
            print(f"Error sending Discord message: {e}")
            return False


def send_leave_request_to_admin(leave_request):
    """Send leave request details to admin channel (general) with action buttons"""
    
    # Determine if paid or unpaid
    paid_status = "Paid" if leave_request.is_paid else "Unpaid"
    leave_type_display = leave_request.get_leave_type_display()
    
    # Convert to Nepal time (Asia/Kathmandu UTC+5:45)
    nepal_tz = pytz.timezone('Asia/Kathmandu')
    applied_datetime_nepal = leave_request.applied_at.astimezone(nepal_tz)
    applied_datetime = applied_datetime_nepal.strftime('%d %B %Y at %I:%M %p')
    
    # Color coding: Blue for pending requests
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
    
    components = [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Approve", "custom_id": f"leave_approve_{leave_request.id}"},
                    {"type": 2, "style": 4, "label": "Reject", "custom_id": f"leave_reject_{leave_request.id}"},
                ],
            }
        ]
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_discord_message(
                channel_id=ADMIN_CHANNEL_ID,
                channel_name=ADMIN_CHANNEL_NAME,
                embed_dict=embed,
                components=components,
            )
        )
        loop.close()
        return result
    except Exception as e:
        print(f"Error in send_leave_request_to_admin: {e}")
        return False


def send_approved_leave_to_employees(leave_request):
    """
    Send approved leave to employee channel in simple format with date header.
    Format: 
    2nd January
    1. Name - Leave Type (Session if not Full Day)
    
    """
    # Get leave type
    leave_type = leave_request.get_leave_type_display()
    
    # Get session - only add if not Full Day
    session = leave_request.get_session_display()
    
    # Format today's date with ordinal suffix
    def _ordinal(n):
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"
    
    today = datetime.now().date()
    date_header = f"**{_ordinal(today.day)} {today.strftime('%B')}**"
    
    if session == "Full Day":
        employee_entry = f"{leave_request.employee.user.get_full_name()} - {leave_type}"
    else:
        employee_entry = f"{leave_request.employee.user.get_full_name()} - {leave_type} ({session})"
    
    # Combine date header with employee entry
    content = f"{date_header}\n{employee_entry}"
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_discord_message(
                channel_id=EMPLOYEE_CHANNEL_ID,
                channel_name=EMPLOYEE_CHANNEL_NAME,
                content=content,
            )
        )
        loop.close()
        return result
    except Exception as e:
        print(f"Error in send_approved_leave_to_employees: {e}")
        return False


def send_daily_summary(approved_leaves_by_date):
    """
    Send daily summary of approved leaves to the admin summary channel.
    approved_leaves_by_date: dict like {date: [LeaveRequest, ...]}
    """
    if not approved_leaves_by_date:
        return False

    def _ordinal(n):
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"

    # Build concise daily summary with numbered items, per date
    lines = []

    for date_obj in sorted(approved_leaves_by_date.keys()):
        leaves = approved_leaves_by_date[date_obj]
        formatted_date = f"{_ordinal(date_obj.day)} {date_obj.strftime('%B')}"
        lines.append(formatted_date)

        for idx, leave in enumerate(leaves, start=1):
            leave_type = leave.get_leave_type_display()
            session = leave.get_session_display()
            
            # Add session info if not Full Day
            if session == "Full Day":
                lines.append(f"{idx}. {leave.employee.user.get_full_name()} - {leave_type}")
            else:
                lines.append(f"{idx}. {leave.employee.user.get_full_name()} - {leave_type} ({session})")

        lines.append("")  # blank line between dates

    message = "\n".join(lines).strip()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_discord_message(
                channel_id=EMPLOYEE_CHANNEL_ID,
                channel_name=SUMMARY_CHANNEL_NAME or EMPLOYEE_CHANNEL_NAME,
                content=message,
            )
        )
        loop.close()
        return result
    except Exception as e:
        print(f"Error in send_daily_summary: {e}")
        return False


def send_rejection_email_to_employee(leave_request):
    """Send rejection email to employee with the rejection reason"""
    try:
        employee = leave_request.employee
        user_email = employee.user.email
        
        if not user_email:
            print(f"No email found for employee {employee.user.get_full_name()}")
            return False
        
        subject = f"Leave Request Rejected - {leave_request.get_leave_type_display()}"
        
        # Create email content
        start_date = leave_request.start_date.strftime("%d %B %Y")
        end_date = leave_request.end_date.strftime("%d %B %Y")
        rejection_reason = leave_request.rejection_reason or "No reason provided"
        
        message = f"""
Dear {employee.user.get_full_name()},

We regret to inform you that your leave request has been rejected.

**Leave Details:**
- Leave Type: {leave_request.get_leave_type_display()}
- Start Date: {start_date}
- End Date: {end_date}
- Session: {leave_request.get_session_display()}

**Reason for Rejection:**
{rejection_reason}

Please contact HR if you have any questions regarding this rejection.

Best regards,
HR Team
        """
        
        # Send email
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL if hasattr(settings, 'DEFAULT_FROM_EMAIL') else 'kharelramit@gmail.com',
            recipient_list=[user_email],
            fail_silently=False,
        )
        
        print(f"Rejection email sent to {user_email}")
        return True
        
    except Exception as e:
        print(f"Error sending rejection email: {e}")
        return False