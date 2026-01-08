import aiohttp
import asyncio
import os
from datetime import datetime
from django.conf import settings
from dotenv import load_dotenv

# Load environment variables from .env if present
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

            # If unknown channel and a channel_name is provided, try resolving by name once and retry
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
    
    embed = {
        "title": f" New Leave Request - {leave_request.employee.name}",
        "color": 3447003, 
        "fields": [
            {
                "name": "Employee",
                "value": leave_request.employee.name,
                "inline": True
            },
            {
                "name": "Leave Type",
                "value": leave_request.get_leave_type_display(),
                "inline": True
            },
            {
                "name": "Start Date",
                "value": leave_request.start_date.strftime("%d %B %Y"),
                "inline": True
            },
            {
                "name": "End Date",
                "value": leave_request.end_date.strftime("%d %B %Y"),
                "inline": True
            },
            {
                "name": "Session",
                "value": leave_request.get_session_display(),
                "inline": True
            },
            {
                "name": "Days",
                "value": str(leave_request.total_days()),
                "inline": True
            },
            {
                "name": "Reason",
                "value": leave_request.reason or "No reason provided",
                "inline": False
            },
            {
                "name": "Status",
                "value": leave_request.get_status_display(),
                "inline": True
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
                    {"type": 2, "style": 1, "label": "Confirm", "custom_id": f"leave_confirm_{leave_request.id}"},
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
    """Send approved leave to employee channel (playground)"""
  
    start_label = f"{leave_request.start_date.day} {leave_request.start_date.strftime('%B')}"
    end_label = f"{leave_request.end_date.day} {leave_request.end_date.strftime('%B')}"
    date_range = start_label if leave_request.start_date == leave_request.end_date else f"{start_label} → {end_label}"
    session_label = leave_request.get_session_display()

    content = (
        f"**{start_label}**\n"
        f"• {leave_request.employee.name} - {leave_request.get_leave_type_display()} "
        f"({session_label}, {date_range})"
    )
    
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
    Send daily summary of approved leaves to employee channel
    approved_leaves_by_date: dict like {date: [LeaveRequest, ...]}
    """
    
    if not approved_leaves_by_date:
        return False
    
    message = ""
    for date_obj in sorted(approved_leaves_by_date.keys()):
        leaves = approved_leaves_by_date[date_obj]
        formatted_date = date_obj.strftime("%d %B")
        message += f"\n**{formatted_date}**\n"
        
        for leave in leaves:
            message += f"• {leave.employee.name} - {leave.get_leave_type_display()}\n"
        
        message += "\n"
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_discord_message(
                channel_id=EMPLOYEE_CHANNEL_ID,
                channel_name=EMPLOYEE_CHANNEL_NAME,
                content=message,
            )
        )
        loop.close()
        return result
    except Exception as e:
        print(f"Error in send_daily_summary: {e}")
        return False
