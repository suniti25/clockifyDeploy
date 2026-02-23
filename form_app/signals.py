import logging
import threading

from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from form_app.models import LeaveRequest


@receiver(post_delete, sender=LeaveRequest)
def delete_google_event_on_leave_delete(sender, instance, **kwargs):
    """
    Delete the corresponding Google Calendar event when a LeaveRequest is deleted.
    """
    if not instance.google_event_id:
        return

    event_ids = [
        eid.strip() for eid in str(instance.google_event_id).split(",") if eid.strip()
    ]
    if not event_ids:
        return
    try:
        from integrations.services import _get_calendar_credential, _build_credentials
        from googleapiclient.discovery import build

        cred = _get_calendar_credential(getattr(instance.employee, "user", None))
        if not cred:
            return
        creds = _build_credentials(cred)
        if not creds:
            return
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        for event_id in event_ids:
            try:
                service.events().delete(
                    calendarId=cred.calendar_id, eventId=event_id
                ).execute()
                logger.info(
                    "Deleted Google Calendar event %s for LeaveRequest %s",
                    event_id,
                    instance.id,
                )
            except Exception:
                logger.exception(
                    "Failed to delete Google Calendar event %s for LeaveRequest %s",
                    event_id,
                    instance.id,
                )
    except Exception:
        logger.exception(
            f"Failed to delete Google Calendar event for LeaveRequest {instance.id}"
        )


logger = logging.getLogger(__name__)


@receiver(post_save, sender=LeaveRequest)
def notify_on_leave_approval(sender, instance, created, **kwargs):

    # Only act on updates (not creation) and only when approved.
    # Avoid re-triggering on unrelated saves (e.g., updating google_event_id).
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return

    if created or instance.status not in ("APPROVED", "REJECTED"):
        return

    def _notify_discord():
        try:
            from discord_app.services import (
                send_approved_leave_to_employees,
                send_rejected_leave_to_employees,
            )

            if instance.status == "APPROVED":
                send_approved_leave_to_employees(instance)
            elif instance.status == "REJECTED":
                send_rejected_leave_to_employees(instance)
        except ImportError:
            logger.warning(
                "Discord service not available. Skipping leave decision notification."
            )
        except Exception:
            logger.exception("Failed to send leave decision notification to Discord")

    try:
        transaction.on_commit(
            lambda: threading.Thread(target=_notify_discord, daemon=True).start()
        )
    except Exception:
        _notify_discord()

    if instance.status == "APPROVED":

        def _sync_google():
            try:
                from integrations.services import sync_approved_leave_to_google

                sync_approved_leave_to_google(instance)
            except Exception:
                logger.exception("Failed to sync approved leave to Google Calendar")

        # Best-effort Google Calendar sync for approved leaves (defer off the request thread).
        try:
            transaction.on_commit(
                lambda: threading.Thread(target=_sync_google, daemon=True).start()
            )
        except Exception:
            _sync_google()
