import aiohttp
import asyncio
import os
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv
from django.conf import settings
from django.core.mail import send_mail

load_dotenv()

logger = logging.getLogger(__name__)

# SAFE ENV HELPERS

def _get_int_env(name: str, default: int = 0, *, strict: bool = False) -> int:
    """
    Safe env int parser:
    - missing/blank -> default (or raise if strict=True)
    - invalid -> default (or raise if strict=True)
    """
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        if strict:
            raise RuntimeError(f"Missing required env var: {name}")
        return default

    try:
        return int(raw)
    except ValueError:
        if strict:
            raise RuntimeError(f"Invalid integer for env var {name}: {raw!r}")
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default


def _run_async(coro):
    """
    Run async code from sync Django context safely.
    If an event loop is already running, create a fresh loop to avoid runtime errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()

    return asyncio.run(coro)

# ENV CONFIG

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

ADMIN_CHANNEL_ID = _get_int_env("DISCORD_ADMIN_CHANNEL_ID", 0)
EMPLOYEE_CHANNEL_ID = _get_int_env("DISCORD_EMPLOYEE_CHANNEL_ID", 0)

DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
ADMIN_CHANNEL_NAME = os.getenv("DISCORD_ADMIN_CHANNEL_NAME")
EMPLOYEE_CHANNEL_NAME = os.getenv("DISCORD_EMPLOYEE_CHANNEL_NAME")
SUMMARY_CHANNEL_NAME = os.getenv("DISCORD_LEAVE_SUMMARY_CHANNEL_NAME", "leaves_and_notices")

_channel_cache = {}

# CHANNEL RESOLUTION

async def resolve_channel_id_by_name(session: aiohttp.ClientSession, channel_name: str):
    """
    Resolve a Discord channel ID by name in the configured guild.
    Returns channel_id (int) or None.
    """
    if not DISCORD_TOKEN or not DISCORD_GUILD_ID or not channel_name:
        return None

    key = f"{DISCORD_GUILD_ID}:{channel_name.lower()}"
    if key in _channel_cache:
        return _channel_cache[key]

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            logger.warning("Failed to fetch guild channels. status=%s", resp.status)
            return None

        channels = await resp.json()
        for ch in channels:
            if ch.get("name", "").lower() == channel_name.lower():
                ch_id = ch.get("id")
                if ch_id:
                    # Discord returns string IDs
                    ch_id_int = int(ch_id)
                    _channel_cache[key] = ch_id_int
                    return ch_id_int

    return None

# CORE DISCORD SEND


async def send_discord_message(
    channel_id=None,
    channel_name=None,
    embed=None,
    content=None,
    components=None,
):
    """
    Send a Discord message to a channel (by ID or name).
    Returns message_id (str) or None.
    """
    if not DISCORD_TOKEN:
        logger.warning("DISCORD_TOKEN not configured; skipping Discord send.")
        return None

    async with aiohttp.ClientSession() as session:
        # Resolve channel by name if needed
        if not channel_id and channel_name:
            channel_id = await resolve_channel_id_by_name(session, channel_name)

        if not channel_id:
            logger.warning("No channel_id resolved/provided; skipping Discord send.")
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
                body = await resp.text()
                logger.error("Discord send failed. status=%s body=%s", resp.status, body)
                return None

            data = await resp.json()
            return data.get("id")

# UPDATE ADMIN MESSAGE


def update_admin_leave_message(leave):
    """
    Updates the admin Discord message (embed + buttons) for a leave request.
    """
    if not DISCORD_TOKEN:
        logger.warning("DISCORD_TOKEN not configured; skipping Discord update.")
        return False

    if not leave.discord_message_id:
        logger.warning("No discord_message_id for leave %s", leave.id)
        return False

    # Prefer numeric ADMIN_CHANNEL_ID
    channel_id = ADMIN_CHANNEL_ID or None

    nepal_tz = pytz.timezone("Asia/Kathmandu")
    applied_time = leave.applied_at.astimezone(nepal_tz)

    paid_status = "Paid" if leave.is_paid else "Unpaid"
    employee_name = leave.employee.user.get_full_name() or leave.employee.user.username

    embed = {
        "title": f"{employee_name} - {paid_status} {leave.get_leave_type_display()} Leave Request",
        "color": 3447003,
        "fields": [
            {"name": "Employee Name", "value": employee_name, "inline": False},
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

    async def _update():
        async with aiohttp.ClientSession() as session:
            # Resolve channel if we don't have id
            resolved_channel_id = channel_id
            if not resolved_channel_id and ADMIN_CHANNEL_NAME:
                resolved_channel_id = await resolve_channel_id_by_name(session, ADMIN_CHANNEL_NAME)

            if not resolved_channel_id:
                logger.warning("Could not resolve admin channel id; skipping update.")
                return False

            url = f"https://discord.com/api/v10/channels/{resolved_channel_id}/messages/{leave.discord_message_id}"
            headers = {
                "Authorization": f"Bot {DISCORD_TOKEN}",
                "Content-Type": "application/json",
            }

            async with session.patch(url, json={"embeds": [embed], "components": components}, headers=headers) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    logger.error("Discord update failed. status=%s body=%s", resp.status, body)
                    return False
                return True

    try:
        return bool(_run_async(_update()))
    except Exception:
        logger.exception("Exception during Discord update for leave %s", leave.id)
        return False


# BACKWARD-COMPATIBILITY
update_discord_leave_message = update_admin_leave_message

# SEND LEAVE TO ADMIN


def send_leave_request_to_admin(leave):
    """
    Sends a leave request embed (with Approve/Reject buttons) to admin channel.
    Saves returned discord_message_id on LeaveRequest.
    """
    nepal_tz = pytz.timezone("Asia/Kathmandu")
    applied_time = leave.applied_at.astimezone(nepal_tz)

    paid_status = "Paid" if leave.is_paid else "Unpaid"
    employee_name = leave.employee.user.get_full_name() or leave.employee.user.username

    embed = {
        "title": f"{employee_name} - {paid_status} {leave.get_leave_type_display()} Leave Request",
        "color": 3447003,
        "fields": [
            {"name": "Employee Name", "value": employee_name, "inline": False},
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

    message_id = _run_async(
        send_discord_message(
            channel_id=ADMIN_CHANNEL_ID or None,
            channel_name=ADMIN_CHANNEL_NAME,
            embed=embed,
            components=components,
        )
    )

    if message_id:
        leave.discord_message_id = message_id
        leave.save(update_fields=["discord_message_id"])

    return message_id

# SEND APPROVED LEAVE TO EMPLOYEES

def send_approved_leave_to_employees(leave):
    today = datetime.now().strftime("%d %B")

    employee_name = leave.employee.user.get_full_name() or leave.employee.user.username
    entry = f"{employee_name} - {leave.get_leave_type_display()}"
    if leave.get_session_display() != "Full Day":
        entry += f" ({leave.get_session_display()})"

    content = f"**{today}**\n{entry}"

    return _run_async(
        send_discord_message(
            channel_id=EMPLOYEE_CHANNEL_ID or None,
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
            employee_name = leave.employee.user.get_full_name() or leave.employee.user.username
            line = f"{i}. {employee_name} - {leave.get_leave_type_display()}"
            if leave.get_session_display() != "Full Day":
                line += f" ({leave.get_session_display()})"
            lines.append(line)

        lines.append("")

    return _run_async(
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

    name = (user.get_full_name() or user.username).strip()

    send_mail(
        subject="Leave Request Rejected",
        message=f"""
Dear {name},

Your leave request has been rejected.

Leave Type: {leave.get_leave_type_display()}
Dates: {leave.start_date} to {leave.end_date}
Session: {leave.get_session_display()}

Reason:
{leave.rejection_reason or "No reason provided"}

Regards,
HR Team
""".strip(),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )

    return True
