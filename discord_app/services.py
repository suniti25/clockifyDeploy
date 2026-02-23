import logging
from datetime import date

import aiohttp
from asgiref.sync import async_to_sync

from django.conf import settings
from django.core.mail import send_mail
from django.utils.html import escape
from django.utils import timezone

from form_app.models import LeaveRequest
from form_app.helpers import display_is_paid

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)


# CORE DISCORD HTTP
async def _discord_request(method: str, url: str, payload: dict | None):
    if not settings.DISCORD_TOKEN:
        logger.warning(
            "settings.DISCORD_TOKEN not configured; skipping Discord request."
        )
        return None

    headers = {
        "Authorization": f"Bot {settings.DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.request(
                method, url, json=payload, headers=headers
            ) as resp:
                text = await resp.text()

                if resp.status not in (200, 201, 204):
                    logger.error(
                        "Discord API failed: %s %s status=%s body=%s",
                        method,
                        url,
                        resp.status,
                        text,
                    )
                    return None

                if resp.status == 204:
                    return {"ok": True}

                try:
                    return await resp.json()
                except Exception:
                    return {"raw": text}

    except Exception:
        logger.exception("Discord request error: %s %s", method, url)
        return None


async def send_discord_message(
    *, channel_id: int | None, embed=None, content=None, components=None
):
    if not channel_id:
        logger.warning("No channel_id provided; skipping Discord send.")
        return None

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"

    payload: dict = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if components:
        payload["components"] = components

    data = await _discord_request("POST", url, payload)
    if not data:
        return None
    return data.get("id")


async def patch_discord_message(
    channel_id: int, message_id: str, *, content=None, embeds=None, components=None
) -> bool:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"

    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if embeds is not None:
        payload["embeds"] = embeds
    if components is not None:
        payload["components"] = components

    data = await _discord_request("PATCH", url, payload)
    return bool(data)


# HELPERS
def _employee_name(leave: LeaveRequest) -> str:
    u = leave.employee.user
    return (u.get_full_name() or u.username or "Unknown").strip()


def _employee_project(leave: LeaveRequest) -> str:
    emp = leave.employee
    for attr in ("current_project", "project", "project_name", "assigned_project"):
        val = getattr(emp, attr, None)
        if val:
            return str(val)

    prof = getattr(emp.user, "profile", None)
    if prof:
        for attr in ("project", "project_name", "current_project"):
            val = getattr(prof, attr, None)
            if val:
                return str(val)

    return "—"


def _format_leave_duration_days(days: float) -> str:
    try:
        d = float(days)
    except (TypeError, ValueError):
        return ""

    if d.is_integer():
        n = int(d)
        return f"{n} day" if n == 1 else f"{n} days"
    return f"{d:g} days"


def _fmt_mdY(d: date | None) -> str:
    return d.strftime("%m-%d-%Y") if d else "—"


def _fmt_nepal_datetime(dt) -> str:
    if not dt:
        return "—"
    local_dt = timezone.localtime(dt)
    return local_dt.strftime("%d %B %Y at %I:%M %p")


def _approval_components(leave_id: int):
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"leave_approve_{leave_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Reject",
                    "custom_id": f"leave_reject_{leave_id}",
                },
            ],
        }
    ]


# EMBED BUILDER
def _build_leave_embed(leave: LeaveRequest) -> dict:
    employee_name = _employee_name(leave)
    project = _employee_project(leave)

    paid_status = display_is_paid(leave.leave_type, getattr(leave, "is_paid", None))
    is_reapply = bool(getattr(leave, "reapplied_from_id", None))

    title_prefix = "RESUBMIT • " if is_reapply else ""
    if paid_status is None:
        title = f"{title_prefix}{employee_name} - {leave.get_leave_type_display()} Leave Request"
    else:
        title = f"{title_prefix}{employee_name} - {'Paid' if paid_status else 'Unpaid'} {leave.get_leave_type_display()} Leave Request"

    from_to = f"{_fmt_mdY(leave.start_date)} → {_fmt_mdY(leave.end_date)}"
    applied_str = _fmt_nepal_datetime(getattr(leave, "applied_at", None))

    fields: list[dict] = []

    if is_reapply:
        fields.append({"name": "Reapplied", "value": "Yes", "inline": False})

    fields.extend(
        [
            {"name": "Employee Name", "value": employee_name, "inline": False},
            {"name": "Project", "value": project, "inline": False},
            {"name": "From – To (MM-DD-YYYY)", "value": from_to, "inline": False},
            {
                "name": "Leave Type",
                "value": leave.get_leave_type_display(),
                "inline": True,
            },
            {"name": "Session", "value": leave.get_session_display(), "inline": True},
        ]
    )

    if paid_status is not None:
        fields.append(
            {
                "name": "Paid/Unpaid",
                "value": "Paid" if paid_status else "Unpaid",
                "inline": True,
            }
        )

    fields.extend(
        [
            {"name": "Total Day/s", "value": str(leave.total_days()), "inline": False},
            {"name": "Reason", "value": leave.reason or "—", "inline": False},
            {"name": "Status", "value": leave.status, "inline": False},
            {"name": "Date of submission", "value": applied_str, "inline": False},
        ]
    )

    return {
        "title": title,
        "color": 0x3498DB,
        "fields": fields,
    }


# ADMIN MESSAGE HANDLING
def send_leave_request_to_admin(leave: LeaveRequest):
    embed = _build_leave_embed(leave)
    components = _approval_components(leave.id) if leave.status == "PENDING" else []

    msg_id = async_to_sync(send_discord_message)(
        channel_id=settings.DISCORD_ADMIN_CHANNEL_ID,
        embed=embed,
        components=components,
    )

    if msg_id:
        leave.discord_message_id = msg_id
        leave.save(update_fields=["discord_message_id"])

    return msg_id


def update_admin_leave_message(leave: LeaveRequest):
    logger.debug(
        "update_admin_leave_message called leave_id=%s discord_message_id=%s status=%s",
        leave.id,
        leave.discord_message_id,
        leave.status,
    )
    if not leave.discord_message_id:
        logger.debug(
            "No discord_message_id; skipping update_admin_leave_message leave_id=%s",
            leave.id,
        )
        return False
    if not settings.DISCORD_ADMIN_CHANNEL_ID:
        logger.warning(
            "DISCORD_settings.DISCORD_ADMIN_CHANNEL_ID not configured; skipping message update."
        )
        return False

    embed = _build_leave_embed(leave)
    components = _approval_components(leave.id) if leave.status == "PENDING" else []

    result = bool(
        async_to_sync(patch_discord_message)(
            settings.DISCORD_ADMIN_CHANNEL_ID,
            leave.discord_message_id,
            embeds=[embed],
            components=components,
        )
    )
    logger.debug(
        "patch_discord_message result=%s leave_id=%s discord_message_id=%s",
        result,
        leave.id,
        leave.discord_message_id,
    )
    return result


update_discord_leave_message = update_admin_leave_message

# DAILY QUERY HELPERS


def _active_today_qs(target_date: date):
    """Approved leaves that are ACTIVE today (on leave today)."""
    return (
        LeaveRequest.objects.filter(
            status="APPROVED",
            start_date__lte=target_date,
            end_date__gte=target_date,
        )
        .select_related("employee", "employee__user")
        .order_by("employee__user__first_name", "employee__user__last_name")
    )


def _approved_today_qs(target_date: date):
    """Leaves that were APPROVED today."""
    return (
        LeaveRequest.objects.filter(
            status="APPROVED",
            approved_at__date=target_date,
        )
        .select_related("employee", "employee__user")
        .order_by("employee__user__first_name", "employee__user__last_name")
    )


# EMPLOYEE CHANNEL
def send_approved_leave_to_employees(leave: LeaveRequest) -> bool:
    if not settings.DISCORD_ADMIN_CHANNEL_ID:
        logger.warning(
            "DISCORD_settings.DISCORD_ADMIN_CHANNEL_ID not configured; skipping approval announcement."
        )
        return False

    label = leave.get_leave_type_display()
    dates = f"{_fmt_mdY(leave.start_date)} to {_fmt_mdY(leave.end_date)}"
    session = leave.get_session_display()

    content = (
        "**Leave approved ✅**\n"
        f"{_employee_name(leave)} — {label} — {session}\n"
        f"**{dates}**"
    )

    msg_id = async_to_sync(send_discord_message)(
        channel_id=settings.DISCORD_ADMIN_CHANNEL_ID,
        content=content,
    )

    return bool(msg_id)


def send_rejected_leave_to_employees(leave: LeaveRequest) -> bool:
    if not settings.DISCORD_ADMIN_CHANNEL_ID:
        logger.warning(
            "DISCORD_settings.DISCORD_ADMIN_CHANNEL_ID not configured; skipping rejection announcement."
        )
        return False

    label = leave.get_leave_type_display()
    dates = f"{_fmt_mdY(leave.start_date)} to {_fmt_mdY(leave.end_date)}"
    session = leave.get_session_display()

    content = (
        "**Leave rejected ❌**\n"
        f"{_employee_name(leave)} — {label} — {session}\n"
        f"**{dates}**"
    )

    msg_id = async_to_sync(send_discord_message)(
        channel_id=settings.DISCORD_ADMIN_CHANNEL_ID,
        content=content,
    )

    return bool(msg_id)


def send_employee_on_leave_today():
    today = timezone.localdate()
    qs = _active_today_qs(today).filter(notified_employee_at__isnull=True)

    if not qs.exists():
        return False

    lines = [f"**On Leave Today — {today.strftime('%d %b %Y')}**"]
    for leave in qs:
        label = leave.get_leave_type_display()
        if label.upper() not in ("WFH",):
            label = f"{label} Leave"
        lines.append(
            f"- {_employee_name(leave)} — {label} — {leave.get_session_display()}"
        )

    content = "\n".join(lines)

    msg_id = async_to_sync(send_discord_message)(
        channel_id=settings.DISCORD_EMPLOYEE_CHANNEL_ID,
        content=content,
    )

    if not msg_id:
        return False

    qs.update(notified_employee_at=timezone.now())
    return True


# ADMIN CHANNEL
def send_admin_approved_today():
    today = timezone.localdate()
    qs = _approved_today_qs(today).filter(notified_admin_at__isnull=True)

    if not qs.exists():
        return False

    lines = [f"**Approved Today — {today.strftime('%d %b %Y')}**"]
    for leave in qs:
        lines.append(
            f"- {_employee_name(leave)} — {leave.get_leave_type_display()} — {leave.get_session_display()}"
        )

    content = "\n".join(lines)

    msg_id = async_to_sync(send_discord_message)(
        channel_id=settings.DISCORD_ADMIN_CHANNEL_ID,
        content=content,
    )

    if not msg_id:
        return False

    qs.update(notified_admin_at=timezone.now())
    return True


# EMAILS
def send_approval_email_to_employee(leave: LeaveRequest):
    user = leave.employee.user
    if not user.email:
        return False

    name = _employee_name(leave)
    name_html = escape(name)

    is_wfh = (leave.leave_type or "").strip().upper() == "WFH"

    leave_type_display = leave.get_leave_type_display()
    session_display = leave.get_session_display()
    dates_display = f"{leave.start_date:%d %b %Y} to {leave.end_date:%d %b %Y}"

    leave_type_html = escape(leave_type_display)
    session_html = escape(session_display)
    dates_html = escape(dates_display)

    duration_days = _format_leave_duration_days(leave.total_days())
    duration_line = f"Duration Taken: {duration_days}" if duration_days else ""
    duration_html_line = (
        f"<strong>Duration Taken:</strong> {escape(duration_days)}<br/>"
        if duration_days
        else ""
    )

    paid_status = display_is_paid(leave.leave_type, getattr(leave, "is_paid", None))
    paid_line = ""
    paid_html_line = ""
    if paid_status is True:
        paid_line = "Paid Status: Paid"
        paid_html_line = "<strong>Paid Status:</strong> Paid<br/>"
    elif paid_status is False:
        paid_line = "Paid Status: Unpaid"
        paid_html_line = "<strong>Paid Status:</strong> Unpaid<br/>"

    approval_reason = (getattr(leave, "approval_reason", None) or "").strip()
    approval_reason_line = (
        f"Admin Approval Reason: {approval_reason}" if approval_reason else ""
    )
    approval_reason_html = escape(approval_reason).replace("\n", "<br/>")
    approval_reason_html_line = (
        f"<strong>Admin Approval Reason:</strong> {approval_reason_html}<br/>"
        if approval_reason
        else ""
    )

    details_text = "\n".join(
        x
        for x in (
            f"Leave Type: {leave_type_display}",
            f"Dates: {dates_display}",
            f"Session: {session_display}",
            duration_line,
        )
        if x
    )

    details_html = (
        f"<strong>Leave Type:</strong> {leave_type_html}<br/>"
        f"<strong>Dates:</strong> {dates_html}<br/>"
        f"<strong>Session:</strong> {session_html}<br/>"
        f"{duration_html_line}"
    )

    if is_wfh:
        subject = "Work From Home Request Approved"
        message = f"""
Dear {name},

Your work from home request has been approved.

{details_text}
{approval_reason_line}

Please remain reachable during working hours and follow the WFH guidelines.

Regards,
Avinto Admin Team
""".strip()

        html_message = f"""
<p>Dear <strong>{name_html}</strong>,</p>
<p>Your work from home request has been approved.</p>
<p>
    {details_html}
    {approval_reason_html_line}
</p>
<p>Please remain reachable during working hours and follow the WFH guidelines.</p>
<p>Regards,<br/>Avinto Admin Team</p>
""".strip()
    else:
        subject = "Leave Request Approved 🎉"
        message = f"""
Dear {name},

Your leave request has been approved.

{details_text}
{paid_line}
{approval_reason_line}

Thank you.

Regards,
Avinto Admin Team
""".strip()

        html_message = f"""
<p>Dear <strong>{name_html}</strong>,</p>
<p>Your leave request has been approved.</p>
<p>
    {details_html}
    {paid_html_line}
    {approval_reason_html_line}
</p>
<p>Thank you.</p>
<p>Regards,<br/>Avinto Admin Team</p>
""".strip()
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        html_message=html_message,
        fail_silently=False,
    )
    return True


def send_rejection_email_to_employee(leave: LeaveRequest):
    user = leave.employee.user
    if not user.email:
        return False

    name = _employee_name(leave)
    name_html = escape(name)
    is_wfh = (leave.leave_type or "").strip().upper() == "WFH"

    leave_type_display = leave.get_leave_type_display()
    session_display = leave.get_session_display()
    dates_display = f"{leave.start_date:%d %b %Y} to {leave.end_date:%d %b %Y}"

    leave_type_html = escape(leave_type_display)
    session_html = escape(session_display)
    dates_html = escape(dates_display)

    duration_days = _format_leave_duration_days(leave.total_days())
    duration_line = f"Duration Taken: {duration_days}" if duration_days else ""
    duration_html_line = (
        f"<strong>Duration Taken:</strong> {escape(duration_days)}<br/>"
        if duration_days
        else ""
    )

    rejection_reason = (
        getattr(leave, "rejection_reason", None) or "No reason provided"
    ).strip()
    rejection_reason_html = escape(rejection_reason).replace("\n", "<br/>")

    details_text = "\n".join(
        x
        for x in (
            f"Leave Type: {leave_type_display}",
            f"Dates: {dates_display}",
            f"Session: {session_display}",
            duration_line,
        )
        if x
    )

    details_html = (
        f"<strong>Leave Type:</strong> {leave_type_html}<br/>"
        f"<strong>Dates:</strong> {dates_html}<br/>"
        f"<strong>Session:</strong> {session_html}<br/>"
        f"{duration_html_line}"
    )

    if is_wfh:
        subject = "Work From Home Request Rejected"
        message = f"""
Dear {name},

Your work from home request has been rejected.

{details_text}

Reason:
{rejection_reason}

Regards,
Avinto Admin Team
""".strip()

        html_message = f"""
<p>Dear <strong>{name_html}</strong>,</p>
<p>Your work from home request has been rejected.</p>
<p>
    {details_html}
</p>
<p><strong>Reason:</strong><br/>{rejection_reason_html}</p>
<p>Regards,<br/>Avinto Admin Team</p>
""".strip()
    else:
        subject = "Leave Request Rejected"
        message = f"""
Dear {name},

Your leave request has been rejected.

{details_text}

Reason:
{rejection_reason}

Regards,
Avinto Admin Team
""".strip()

        html_message = f"""
<p>Dear <strong>{name_html}</strong>,</p>
<p>Your leave request has been rejected.</p>
<p>
    {details_html}
</p>
<p><strong>Reason:</strong><br/>{rejection_reason_html}</p>
<p>Regards,<br/>Avinto Admin Team</p>
""".strip()

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        html_message=html_message,
        fail_silently=False,
    )
    return True
