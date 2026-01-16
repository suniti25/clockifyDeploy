import aiohttp
import asyncio
import os
import logging
from datetime import datetime
import pytz

from django.conf import settings
from django.core.mail import send_mail
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

ADMIN_CHANNEL_ID = int(os.getenv("DISCORD_ADMIN_CHANNEL_ID", 0))
EMPLOYEE_CHANNEL_ID = int(os.getenv("DISCORD_EMPLOYEE_CHANNEL_ID", 0))

DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
ADMIN_CHANNEL_NAME = os.getenv("DISCORD_ADMIN_CHANNEL_NAME")
EMPLOYEE_CHANNEL_NAME = os.getenv("DISCORD_EMPLOYEE_CHANNEL_NAME")
SUMMARY_CHANNEL_NAME = os.getenv(
    "DISCORD_LEAVE_SUMMARY_CHANNEL_NAME", "leaves_and_notices"
)

_channel_cache = {}

# CHANNEL RESOLUTION

async def resolve_channel_id_by_name(session, channel_name):
    if not DISCORD_GUILD_ID or not channel_name:
        return None

    key = f"{DISCORD_GUILD_ID}:{channel_name.lower()}"
    if key in _channel_cache:
        return _channel_cache[key]

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None

        for ch in await resp.json():
            if ch.get("name", "").lower() == channel_name.lower():
                _channel_cache[key] = ch["id"]
                return ch["id"]

    return None

# CORE DISCORD SEND

async def send_discord_message(
    channel_id=None,
    channel_name=None,
    embed=None,
    content=None,
    components=None,
):
    if not DISCORD_TOKEN:
        return None

    async with aiohttp.ClientSession() as session:

        if not channel_id and channel_name:
            channel_id = await resolve_channel_id_by_name(session, channel_name)

        if not channel_id:
            return None

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
        }

        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        if components:
            payload["components"] = components

        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status not in (200, 201):
                return None

            data = await resp.json()
            return data.get("id")


# SEND LEAVE TO ADMIN

def send_leave_request_to_admin(leave):
    nepal_tz = pytz.timezone("Asia/Kathmandu")
    applied_time = leave.applied_at.astimezone(nepal_tz)

    paid_status = "Paid" if leave.is_paid else "Unpaid"
    embed = {
        "title": f"{leave.employee.user.get_full_name()} - {paid_status} {leave.get_leave_type_display()} Leave Request",
        "color": 3447003,
        "fields": [
            {"name": "Employee Name", "value": leave.employee.user.get_full_name(), "inline": False},
            {"name": "Duration", "value": f"{leave.start_date} to {leave.end_date}", "inline": False},
            {"name": "Leave Type", "value": leave.get_leave_type_display(), "inline": True},
            {"name": "Session", "value": leave.get_session_display(), "inline": True},
            {"name": "Total Day/s", "value": str(leave.total_days()), "inline": True},
            {"name": "Reason", "value": leave.reason or "—", "inline": False},
            {"name": "Date and time of request", "value": applied_time.strftime("%d %B %Y at %I:%M %p"), "inline": False},
        ],
    }

    components = [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": "Approve", "custom_id": f"leave_approve_{leave.id}"},
                {"type": 2, "style": 4, "label": "Reject", "custom_id": f"leave_reject_{leave.id}"},
            ],
        }
    ]

    message_id = asyncio.run(
        send_discord_message(
            channel_id=ADMIN_CHANNEL_ID,
            channel_name=ADMIN_CHANNEL_NAME,
            embed=embed,
            components=components,
        )
    )

    if message_id:
        leave.discord_message_id = message_id
        leave.save(update_fields=["discord_message_id"])

    return message_id

# UPDATE ADMIN MESSAGE 

def update_admin_leave_message(leave):
    if not leave.discord_message_id:
        logger.warning(f"No discord_message_id for leave {leave.id}")
        return False

    logger.info(f"Updating Discord message ID: {leave.discord_message_id} for leave ID: {leave.id}")

    nepal_tz = pytz.timezone("Asia/Kathmandu")
    applied_time = leave.applied_at.astimezone(nepal_tz)
    
    # Keep the same format as the original message
    paid_status = "Paid" if leave.is_paid else "Unpaid"
    embed = {
        "title": f"{leave.employee.user.get_full_name()} - {paid_status} {leave.get_leave_type_display()} Leave Request",
        "color": 3447003,
        "fields": [
            {"name": "Employee Name", "value": leave.employee.user.get_full_name(), "inline": False},
            {"name": "Duration", "value": f"{leave.start_date} to {leave.end_date}", "inline": False},
            {"name": "Leave Type", "value": leave.get_leave_type_display(), "inline": True},
            {"name": "Session", "value": leave.get_session_display(), "inline": True},
            {"name": "Total Day/s", "value": str(leave.total_days()), "inline": True},
            {"name": "Reason", "value": leave.reason or "—", "inline": False},
            {"name": "Date and time of request", "value": applied_time.strftime("%d %B %Y at %I:%M %p"), "inline": False},
        ],
    }

    # Keep the buttons intact
    components = [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": "Approve", "custom_id": f"leave_approve_{leave.id}"},
                {"type": 2, "style": 4, "label": "Reject", "custom_id": f"leave_reject_{leave.id}"},
            ],
        }
    ]

    url = f"https://discord.com/api/v10/channels/{ADMIN_CHANNEL_ID}/messages/{leave.discord_message_id}"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    async def _update():
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                url, json={"embeds": [embed], "components": components}, headers=headers
            ) as resp:
                logger.info(f"Discord API response status: {resp.status}")
                if resp.status not in (200, 204):
                    error_text = await resp.text()
                    logger.error(f"Discord update failed: {error_text}")
                return resp.status in (200, 204)

    try:
        result = asyncio.run(_update())
        logger.info(f"Discord message update result: {result}")
        return result
    except Exception as e:
        logger.error(f"Exception during Discord update: {str(e)}", exc_info=True)
        return False


# BACKWARD-COMPATIBILITY 
update_discord_leave_message = update_admin_leave_message

# SEND APPROVED LEAVE TO EMPLOYEES

def send_approved_leave_to_employees(leave):
    today = datetime.now().strftime("%d %B")

    entry = f"{leave.employee.user.get_full_name()} - {leave.get_leave_type_display()}"
    if leave.get_session_display() != "Full Day":
        entry += f" ({leave.get_session_display()})"

    content = f"**{today}**\n{entry}"

    return asyncio.run(
        send_discord_message(
            channel_id=EMPLOYEE_CHANNEL_ID,
            channel_name=EMPLOYEE_CHANNEL_NAME,
            content=content,
        )
    )

# DAILY SUMMARY

def send_daily_summary(approved_leaves_by_date):
    if not approved_leaves_by_date:
        return False

    lines = []

    for date, leaves in approved_leaves_by_date.items():
        lines.append(date.strftime("%d %B"))

        for i, leave in enumerate(leaves, 1):
            line = f"{i}. {leave.employee.user.get_full_name()} - {leave.get_leave_type_display()}"
            if leave.get_session_display() != "Full Day":
                line += f" ({leave.get_session_display()})"
            lines.append(line)

        lines.append("")

    return asyncio.run(
        send_discord_message(
            channel_name=SUMMARY_CHANNEL_NAME,
            content="\n".join(lines),
        )
    )
# REJECTION EMAIL

def send_rejection_email_to_employee(leave):
    user = leave.employee.user
    if not user.email:
        return False

    send_mail(
        subject="Leave Request Rejected",
        message=f"""
Dear {user.get_full_name()},

Your leave request has been rejected.

Leave Type: {leave.get_leave_type_display()}
Dates: {leave.start_date} to {leave.end_date}
Session: {leave.get_session_display()}

Reason:
{leave.rejection_reason or "No reason provided"}

Regards,
HR Team
""",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )

    return True
