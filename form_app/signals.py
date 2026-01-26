from asyncio.log import logger
from django.db.models.signals import post_save
from django.dispatch import receiver
from form_app.models import LeaveRequest
import logging


@receiver(post_save, sender=LeaveRequest)
def notify_on_leave_approval(sender, instance, created, update_fields, **kwargs):
    """
    Send Discord notification when a leave request is approved
    """
    if not created and update_fields and 'status' in update_fields:
        
        if instance.status == 'APPROVED':
            
            try:
                from discord_app.services import send_approved_leave_to_employees
                send_approved_leave_to_employees(instance)
            except ImportError:
                logger = logging.getLogger(__name__)

                logger.warning("Discord service not available")
