from __future__ import annotations

from datetime import timedelta
import logging

from django.conf import settings

from integrations.crypto import decrypt_str
from integrations.models import GoogleCalendarCredential
from form_app.models import LeaveRequest

logger = logging.getLogger(__name__)


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
        scopes=["https://www.googleapis.com/auth/calendar.events"],
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


def _event_payload(leave: LeaveRequest) -> dict:
    employee = leave.employee
    name = getattr(employee, "name", "") or (getattr(employee.user, "get_full_name", lambda: "")() if employee and employee.user else "")
    if not name:
        name = getattr(employee.user, "username", "") if employee and employee.user else "Unknown"

    start_date = leave.start_date
    end_date_exclusive = (leave.end_date or leave.start_date) + timedelta(days=1)

    session = (leave.session or "FULL").strip().upper()

    description_parts = [f"Employee: {name}", f"Leave Type: {(leave.leave_type or '').strip().upper()}"]
    if session in ("AM", "PM"):
        description_parts.append(f"Session: {session}")
    if leave.reason:
        description_parts.append(f"Reason: {leave.reason}")

    leave_type_label = (leave.leave_type or "").strip().upper() or "LEAVE"

    return {
        "summary": f"{leave_type_label.title()} Leave - {name}",
        "description": "\n".join(description_parts),
        "start": {"date": start_date.isoformat()},
        "end": {"date": end_date_exclusive.isoformat()},
    }


def sync_approved_leave_to_google(leave: LeaveRequest, user=None) -> bool:
    if not leave or (leave.status or "").strip().upper() != "APPROVED":
        logger.info("Skip Google sync: leave not approved (id=%s)", getattr(leave, "id", None))
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

    from datetime import timedelta
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        old_event_ids = []
        if leave.google_event_id:
            old_event_ids = [eid for eid in leave.google_event_id.split(",") if eid.strip()]
            for eid in old_event_ids:
                try:
                    service.events().delete(calendarId=cred.calendar_id, eventId=eid).execute()
                except Exception as e:
                    logger.warning(f"Failed to delete old Google event {eid}: {e}")
            # Clear event id after deletion
            leave.google_event_id = ""
            leave.save(update_fields=["google_event_id"])

        # Generate all leave dates excluding weekends
        start_date = leave.start_date
        end_date = leave.end_date
        days = (end_date - start_date).days + 1
        leave_dates = [start_date + timedelta(days=i) for i in range(days) if (start_date + timedelta(days=i)).weekday() < 5]

        created_event_ids = []
        for day in leave_dates:
            body = {
                "summary": f"{(leave.leave_type or '').strip().title()} Leave - {getattr(leave.employee, 'name', 'Unknown')}",
                "description": f"Employee: {getattr(leave.employee, 'name', 'Unknown')}\nLeave Type: {(leave.leave_type or '').strip().upper()}" + (f"\nReason: {leave.reason}" if leave.reason else ""),
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
            }
            event = service.events().insert(calendarId=cred.calendar_id, body=body).execute()
            event_id = (event or {}).get("id")
            if event_id:
                created_event_ids.append(event_id)

        # Store all event ids as a comma-separated string
        leave.google_event_id = ",".join(created_event_ids)
        leave.save(update_fields=["google_event_id"])

        logger.info(
            "Synced leave to Google Calendar (id=%s, type=%s, event_ids=%s)",
            leave.id,
            leave.leave_type,
            created_event_ids,
        )
        return True
    except Exception:
        logger.exception("Failed to sync leave to Google Calendar")
        return False
