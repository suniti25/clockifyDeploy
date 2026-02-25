from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from form_app.models import LeaveRequest
from integrations.crypto import decrypt_str
from integrations.models import GoogleCalendarCredential

logger = logging.getLogger(__name__)

KTM_TZ = ZoneInfo("Asia/Kathmandu")
KTM_TZ_NAME = "Asia/Kathmandu"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _get_calendar_credential(user=None) -> GoogleCalendarCredential | None:
    if user:
        cred = GoogleCalendarCredential.objects.filter(user=user).first()
        if cred:
            return cred
    return GoogleCalendarCredential.objects.order_by("id").first()


def _build_credentials(cred: GoogleCalendarCredential):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception:
        logger.exception("Google auth libraries are not installed")
        return None

    refresh_token = decrypt_str(cred.refresh_token_encrypted)

    creds = Credentials(
        token=cred.access_token or None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID or None,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET or None,
        scopes=GOOGLE_SCOPES,
    )

    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
            cred.access_token = creds.token or ""
            cred.token_expiry = creds.expiry
            cred.save(update_fields=["access_token", "token_expiry"])
        except Exception:
            logger.exception("Failed refreshing Google credentials")
            return None

    return creds


def _employee_display_name(leave: LeaveRequest) -> str:
    emp = getattr(leave, "employee", None)

    name = (getattr(emp, "name", "") or "").strip()
    if name:
        return name

    user = getattr(emp, "user", None) if emp else None
    full = (user.get_full_name() or "").strip() if user else ""
    if full:
        return full

    uname = (getattr(user, "username", "") or "").strip() if user else ""
    return uname or "Unknown"


def _normalize_session(raw: str | None) -> str:
    s = (raw or "FULL").strip().upper()
    if s == "FD":
        return "FULL"
    if s == "MORNING":
        return "AM"
    if s == "AFTERNOON":
        return "PM"
    if s in ("AM", "PM", "FULL"):
        return s
    return "FULL"


def _build_event_body_for_day(leave: LeaveRequest, day) -> dict:
    name = _employee_display_name(leave)
    leave_type_title = (leave.leave_type or "").strip().title() or "Leave"
    leave_type_upper = (leave.leave_type or "").strip().upper()
    session = _normalize_session(getattr(leave, "session", None))

    desc = [f"Employee: {name}", f"Leave Type: {leave_type_upper}"]
    if session in ("AM", "PM"):
        desc.append(f"Session: {session}")
    if getattr(leave, "reason", None):
        desc.append(f"Reason: {leave.reason}")

    # FULL => all-day event
    if session == "FULL":
        return {
            "summary": f"{leave_type_title} - {name}",
            "description": "\n".join(desc),
            "start": {"date": day.isoformat()},
            "end": {"date": (day + timedelta(days=1)).isoformat()},
        }

    # AM/PM => timed block
    start_t = time(9, 0) if session == "AM" else time(14, 0)
    end_t = time(13, 0) if session == "AM" else time(18, 0)

    start_dt = datetime.combine(day, start_t, tzinfo=KTM_TZ)
    end_dt = datetime.combine(day, end_t, tzinfo=KTM_TZ)

    half_label = "Morning" if session == "AM" else "Afternoon"

    return {
        "summary": f"{leave_type_title} ({half_label}) - {name}",
        "description": "\n".join(desc),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": KTM_TZ_NAME},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": KTM_TZ_NAME},
    }


def _delete_event_ids(service, calendar_id: str, event_ids: list[str]) -> None:
    for eid in event_ids:
        try:
            service.events().delete(calendarId=calendar_id, eventId=eid).execute()
        except Exception as e:
            logger.warning("Failed to delete Google event %s: %s", eid, e)


def delete_leave_events_from_google(leave: LeaveRequest, user=None) -> bool:
    """
    Deletes events referenced by leave.google_event_id and clears the field.
    """
    if not leave:
        return False

    owner_user = None
    try:
        owner_user = leave.employee.user if leave and leave.employee else None
    except Exception:
        owner_user = None

    cred = _get_calendar_credential(user or owner_user)
    if not cred:
        logger.info("No Google calendar credential found. Skipping delete.")
        return False

    creds = _build_credentials(cred)
    if not creds:
        logger.info("Failed to build Google credentials. Skipping delete.")
        return False

    try:
        from googleapiclient.discovery import build
    except Exception:
        logger.exception("googleapiclient is not installed")
        return False

    try:
        raw = (getattr(leave, "google_event_id", "") or "").strip()
        if not raw:
            return True

        event_ids = [eid.strip() for eid in raw.split(",") if eid.strip()]

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _delete_event_ids(service, cred.calendar_id, event_ids)

        leave.google_event_id = ""
        leave.save(update_fields=["google_event_id"])
        return True
    except Exception:
        logger.exception("Failed to delete leave events from Google Calendar")
        return False


def sync_approved_leave_to_google(leave: LeaveRequest, user=None) -> bool:
    if not leave or (leave.status or "").strip().upper() != "APPROVED":
        logger.info(
            "Skip Google sync: leave not approved (id=%s)", getattr(leave, "id", None)
        )
        return False

    owner_user = None
    try:
        owner_user = leave.employee.user if leave and leave.employee else None
    except Exception:
        owner_user = None

    cred = _get_calendar_credential(user or owner_user)
    if not cred:
        logger.info("No Google calendar credential found. Skipping sync.")
        return False

    creds = _build_credentials(cred)
    if not creds:
        logger.info("Failed to build Google credentials. Skipping sync.")
        return False

    try:
        from googleapiclient.discovery import build
    except Exception:
        logger.exception("googleapiclient is not installed")
        return False

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Delete old events using SAME service (no double build)
        raw_old = (getattr(leave, "google_event_id", "") or "").strip()
        if raw_old:
            old_ids = [eid.strip() for eid in raw_old.split(",") if eid.strip()]
            _delete_event_ids(service, cred.calendar_id, old_ids)
            leave.google_event_id = ""
            leave.save(update_fields=["google_event_id"])

        # Weekday dates only (Mon-Fri)
        start_date = leave.start_date
        end_date = leave.end_date or leave.start_date
        total = (end_date - start_date).days + 1

        leave_dates = [
            start_date + timedelta(days=i)
            for i in range(total)
            if (start_date + timedelta(days=i)).weekday() < 5
        ]

        created_ids: list[str] = []
        for day in leave_dates:
            body = _build_event_body_for_day(leave, day)
            event = (
                service.events()
                .insert(calendarId=cred.calendar_id, body=body)
                .execute()
            )
            eid = (event or {}).get("id")
            if eid:
                created_ids.append(eid)

        leave.google_event_id = ",".join(created_ids)
        leave.save(update_fields=["google_event_id"])

        logger.info(
            "Synced leave to Google Calendar (id=%s, type=%s, event_ids=%s)",
            leave.id,
            leave.leave_type,
            created_ids,
        )
        return True
    except Exception:
        logger.exception("Failed to sync leave to Google Calendar")
        return False
