import logging
from datetime import date, timedelta

import aiohttp
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.html import escape

from form_app.helpers import display_is_paid
from form_app.models import LeaveRequest

from discord_app.models import DiscordDailyMessage

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Keep UA stable. Cloudflare can block "generic" clients if UA is missing.
DISCORD_USER_AGENT = "leave-backend-prod (https://avinto.no, 1.0)"


# CORE DISCORD HTTP
async def _discord_request(method: str, url: str, payload: dict | None):
    """
    Low-level Discord HTTP wrapper.
    Returns:
      - dict (json) on success,
      - {"ok": True} on 204,
      - None on failure.
    """
    token = getattr(settings, "DISCORD_TOKEN", None)
    if not token:
        logger.warning(
            "DISCORD: settings.DISCORD_TOKEN not configured; skipping request."
        )
        return None

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": DISCORD_USER_AGENT,
    }

    payload_keys = list((payload or {}).keys())
    logger.info("DISCORD_REQUEST: %s %s payload_keys=%s", method, url, payload_keys)

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.request(
                method, url, json=payload, headers=headers
            ) as resp:
                text = await resp.text()

                if resp.status not in (200, 201, 204):
                    # Discord/Cloudflare sometimes returns 40333 with message "internal network error"
                    # which is why we log the response body.
                    # Truncate body so logs don't explode.
                    body_preview = (text or "")[:2000]
                    logger.error(
                        "DISCORD_FAILED: %s %s status=%s body=%s payload=%s",
                        method,
                        url,
                        resp.status,
                        body_preview,
                        payload,
                    )
                    return None

                if resp.status == 204:
                    return {"ok": True}

                try:
                    return await resp.json()
                except Exception:
                    return {"raw": (text or "")[:2000]}

    except Exception:
        logger.exception("DISCORD_EXCEPTION: %s %s payload=%s", method, url, payload)
        return None


async def send_discord_message(
    *, channel_id: int | None, embed=None, content=None, components=None
):
    if not channel_id:
        logger.warning("DISCORD_SEND: No channel_id provided; skipping.")
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
    channel_id: int,
    message_id: str,
    *,
    content=None,
    embeds=None,
    components=None,
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


def _count_weekdays_inclusive(start: date | None, end: date | None) -> int:
    if not start or not end or start > end:
        return 0
    cur = start
    count = 0
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


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

    title_prefix = "RESUBMISSION • " if is_reapply else ""
    if paid_status is None:
        title = f"{title_prefix}{employee_name} - {leave.get_leave_type_display()} Leave Request"
    else:
        title = (
            f"{title_prefix}{employee_name} - "
            f"{'Paid' if paid_status else 'Unpaid'} {leave.get_leave_type_display()} Leave Request"
        )

    from_to = f"{_fmt_mdY(leave.start_date)} → {_fmt_mdY(leave.end_date)}"
    applied_str = _fmt_nepal_datetime(getattr(leave, "applied_at", None))

    selected_dates: list[date] = []
    is_selective = False
    try:
        if hasattr(leave, "days") and leave.days.exists():
            selected_dates = list(
                leave.days.order_by("date").values_list("date", flat=True)
            )
            expected = _count_weekdays_inclusive(leave.start_date, leave.end_date)
            # If days don't match the full weekday range, treat as selective/manual.
            is_selective = expected > 0 and len(selected_dates) != expected
    except Exception:
        selected_dates = []
        is_selective = False

    fields: list[dict] = []

    if is_reapply:
        fields.append({"name": "Reapplied", "value": "Yes", "inline": False})

    fields.extend(
        [
            {"name": "Employee Name", "value": employee_name, "inline": False},
            {"name": "Project", "value": project, "inline": False},
            (
                {
                    "name": "From – To (MM-DD-YYYY)",
                    "value": from_to,
                    "inline": False,
                }
                if not is_selective
                else {
                    "name": "Selected dates (MM-DD-YYYY)",
                    "value": "—",
                    "inline": False,
                }
            ),
            {
                "name": "Leave Type",
                "value": leave.get_leave_type_display(),
                "inline": True,
            },
            {"name": "Session", "value": leave.get_session_display(), "inline": True},
        ]
    )

    if is_selective and selected_dates:
        # Keep message readable; avoid overly long embeds.
        shown = selected_dates[:25]
        dates_str = ", ".join(_fmt_mdY(d) for d in shown)
        if len(selected_dates) > len(shown):
            dates_str += f" … (+{len(selected_dates) - len(shown)} more)"
        # Replace the placeholder "Selected dates" field inserted above.
        for f in fields:
            if f.get("name") == "Selected dates (MM-DD-YYYY)":
                f["value"] = dates_str
                break

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

    return {"title": title, "color": 0x3498DB, "fields": fields}


# ADMIN MESSAGE HANDLING
def send_leave_request_to_admin(leave: LeaveRequest):
    if not leave:
        logger.warning("SEND_LEAVE_TO_ADMIN: called with leave=None; skipping")
        return None

    """
    Called when employee applies leave.
    Stores discord_message_id back into LeaveRequest if sent successfully.
    """
    admin_channel_id = getattr(settings, "DISCORD_ADMIN_CHANNEL_ID", None)
    logger.info(
        "SEND_LEAVE_TO_ADMIN: leave_id=%s status=%s admin_channel_id=%s",
        leave.id,
        leave.status,
        admin_channel_id,
    )

    try:
        embed = _build_leave_embed(leave)
        components = _approval_components(leave.id) if leave.status == "PENDING" else []

        msg_id = async_to_sync(send_discord_message)(
            channel_id=admin_channel_id,
            embed=embed,
            components=components,
        )

        logger.info(
            "SEND_LEAVE_TO_ADMIN_RESULT: leave_id=%s msg_id=%s", leave.id, msg_id
        )

        if msg_id:
            leave.discord_message_id = msg_id
            leave.save(update_fields=["discord_message_id"])

        return msg_id

    except Exception:
        logger.exception("SEND_LEAVE_TO_ADMIN_EXCEPTION: leave_id=%s", leave.id)
        return None


def update_admin_leave_message(leave: LeaveRequest):
    logger.debug(
        "update_admin_leave_message called leave_id=%s discord_message_id=%s status=%s",
        leave.id,
        leave.discord_message_id,
        leave.status,
    )

    if not leave.discord_message_id:
        logger.debug("No discord_message_id; skipping update leave_id=%s", leave.id)
        return False

    admin_channel_id = getattr(settings, "DISCORD_ADMIN_CHANNEL_ID", None)
    if not admin_channel_id:
        logger.warning(
            "DISCORD_ADMIN_CHANNEL_ID not configured; skipping message update."
        )
        return False

    embed = _build_leave_embed(leave)
    components = _approval_components(leave.id) if leave.status == "PENDING" else []

    result = bool(
        async_to_sync(patch_discord_message)(
            admin_channel_id,
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
    admin_channel_id = getattr(settings, "DISCORD_ADMIN_CHANNEL_ID", None)
    if not admin_channel_id:
        logger.warning(
            "DISCORD_ADMIN_CHANNEL_ID not configured; skipping approval announcement."
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
        channel_id=admin_channel_id, content=content
    )
    return bool(msg_id)


def send_rejected_leave_to_employees(leave: LeaveRequest) -> bool:
    admin_channel_id = getattr(settings, "DISCORD_ADMIN_CHANNEL_ID", None)
    if not admin_channel_id:
        logger.warning(
            "DISCORD_ADMIN_CHANNEL_ID not configured; skipping rejection announcement."
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
        channel_id=admin_channel_id, content=content
    )
    return bool(msg_id)


def send_employee_on_leave_today():
    return upsert_employee_on_leave_today_message(target_date=timezone.localdate())


def _build_employee_on_leave_today_content(*, target_date: date) -> str:
    qs = _active_today_qs(target_date)

    lines = [f"**On Leave Today — {target_date.strftime('%d %b %Y')}**"]
    if not qs.exists():
        lines.append("- None")
        return "\n".join(lines)

    for leave in qs:
        label = leave.get_leave_type_display()
        lt = (getattr(leave, "leave_type", "") or "").strip().upper()
        if lt != "WFH" and "LEAVE" not in (label or "").strip().upper():
            label = f"{label} Leave"
        lines.append(
            f"- {_employee_name(leave)} — {label} — {leave.get_session_display()}"
        )

    return "\n".join(lines)


def upsert_employee_on_leave_today_message(
    *, target_date: date, create_if_missing: bool = True
) -> bool:
    """Create or edit the *same* daily message in #leaves-and-notices.

    This enables real-time updates after the cron post (e.g., delete after 11am).
    """

    employee_channel_id = getattr(settings, "DISCORD_EMPLOYEE_CHANNEL_ID", None)
    try:
        employee_channel_id_int = int(employee_channel_id)
    except (TypeError, ValueError):
        employee_channel_id_int = 0

    if not employee_channel_id_int:
        logger.warning(
            "DISCORD_EMPLOYEE_CHANNEL_ID not configured; skipping on-leave-today upsert."
        )
        return False

    content = _build_employee_on_leave_today_content(target_date=target_date)

    record = (
        DiscordDailyMessage.objects.filter(
            key=DiscordDailyMessage.KEY_EMPLOYEE_ON_LEAVE_TODAY,
            target_date=target_date,
        )
        .order_by("-id")
        .first()
    )

    if record and record.message_id and record.channel_id:
        ok = bool(
            async_to_sync(patch_discord_message)(
                int(record.channel_id),
                str(record.message_id),
                content=content,
            )
        )
        if ok:
            DiscordDailyMessage.objects.filter(id=record.id).update(
                channel_id=employee_channel_id_int
            )
            return True

        # Fallback: if the stored message was deleted (e.g., Discord 404 Unknown Message),
        # recreate and persist a fresh message id so refresh APIs do not fail permanently.
        if create_if_missing:
            msg_id = async_to_sync(send_discord_message)(
                channel_id=employee_channel_id_int, content=content
            )
            if not msg_id:
                return False

            DiscordDailyMessage.objects.update_or_create(
                key=DiscordDailyMessage.KEY_EMPLOYEE_ON_LEAVE_TODAY,
                target_date=target_date,
                defaults={
                    "channel_id": employee_channel_id_int,
                    "message_id": str(msg_id),
                },
            )
            return True

        return False

    if not create_if_missing:
        return False

    msg_id = async_to_sync(send_discord_message)(
        channel_id=employee_channel_id_int, content=content
    )
    if not msg_id:
        return False

    DiscordDailyMessage.objects.update_or_create(
        key=DiscordDailyMessage.KEY_EMPLOYEE_ON_LEAVE_TODAY,
        target_date=target_date,
        defaults={
            "channel_id": employee_channel_id_int,
            "message_id": str(msg_id),
        },
    )
    return True


# ADMIN CHANNEL DAILY


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

    admin_channel_id = getattr(settings, "DISCORD_ADMIN_CHANNEL_ID", None)
    msg_id = async_to_sync(send_discord_message)(
        channel_id=admin_channel_id, content=content
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

Reason for rejection:
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
<p><strong>Reason for rejection:</strong><br/>{rejection_reason_html}</p>
<p>Regards,<br/>Avinto Admin Team</p>
""".strip()
    else:
        subject = "Leave Request Rejected"
        message = f"""
Dear {name},

Your leave request has been rejected.

{details_text}

Reason for rejection:
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
<p><strong>Reason for rejection:</strong><br/>{rejection_reason_html}</p>
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
