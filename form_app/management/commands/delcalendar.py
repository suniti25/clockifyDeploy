from datetime import datetime, timezone
import logging

from django.core.management.base import BaseCommand

from integrations.services import _get_calendar_credential, _build_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Delete orphaned Google Calendar events for LeaveRequests that no longer exist in the DB."

    @staticmethod
    def _looks_like_leave_event(event: dict) -> bool:
        summary = (event or {}).get("summary") or ""
        description = (event or {}).get("description") or ""

        if " leave - " in summary.lower():
            return True
        if "employee:" in description.lower() and "leave type:" in description.lower():
            return True
        return False

    def handle(self, *args, **options):
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT google_event_id FROM form_app_leaverequest WHERE google_event_id != ''")
            raw_ids = [row[0] for row in cursor.fetchall()]

        existing_event_ids: set[str] = set()
        for raw in raw_ids:
            if not raw:
                continue
            for eid in str(raw).split(","):
                eid = eid.strip()
                if eid:
                    existing_event_ids.add(eid)

        cred = _get_calendar_credential()
        if not cred:
            self.stdout.write(self.style.ERROR("No Google Calendar credential found."))
            return
        creds = _build_credentials(cred)
        if not creds:
            self.stdout.write(self.style.ERROR("Failed to build Google credentials."))
            return
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        calendar_id = cred.calendar_id
        page_token = None
        deleted_count = 0
        scanned_count = 0
        while True:
            events = service.events().list(
                calendarId=calendar_id,
                pageToken=page_token,
                timeMin=datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
                showDeleted=False,
                singleEvents=True,
                maxResults=2500,
            ).execute()
            for event in events.get('items', []):
                scanned_count += 1
                event_id = event.get('id')
                if not event_id:
                    continue
                if not self._looks_like_leave_event(event):
                    continue

                if event_id not in existing_event_ids:
                    try:
                        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
                        logger.info(f"Deleted orphaned Google Calendar event {event_id}")
                        deleted_count += 1
                    except HttpError as e:
                        logger.error(f"Failed to delete event {event_id}: {e}")
            page_token = events.get('nextPageToken')
            if not page_token:
                break
        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {scanned_count} events. Deleted {deleted_count} orphaned leave events."
            )
        )
