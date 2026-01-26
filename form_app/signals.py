import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from form_app.models import LeaveRequest
from discord_app.services import send_leave_request_to_admin

logger = logging.getLogger(__name__)


@receiver(post_save, sender=LeaveRequest)
def notify_on_leave_approval(sender, instance, created, update_fields=None, **kwargs):
    """
    Send Discord notification when a leave request is approved
    """
    try:
        # Only notify when status becomes APPROVED
        if not created and instance.status == "APPROVED":
            send_leave_request_to_admin(instance)
    except Exception:
        logger.exception("Failed to send leave approval notification to Discord")
