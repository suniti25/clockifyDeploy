import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from form_app.models import LeaveRequest

logger = logging.getLogger(__name__)


@receiver(post_save, sender=LeaveRequest)
def notify_on_leave_approval(sender, instance, created, **kwargs):
    """
    Send Discord notification when a leave request is approved.

    IMPORTANT:
    - This should NOT resend the original admin request message
    - It should announce approval or update an existing message instead
    """

    # Only act on updates (not creation) and only when approved
    if created or instance.status != "APPROVED":
        return

    try:
        # Use the correct approval/announcement function
        from discord_app.services import send_approved_leave_to_employees

        send_approved_leave_to_employees(instance)

    except ImportError:
        logger.warning(
            "Discord approval service not available. Skipping approval notification."
        )
    except Exception:
        logger.exception(
            "Failed to send leave approval notification to Discord"
        )
