from datetime import timedelta
import logging

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Profile, Employee

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def create_profile_and_employee(sender, instance, created, **kwargs):
    if not created:
        return

    # Timezone-safe "today"
    today = timezone.localdate()

    # Determine role correctly
    role = "ADMIN" if instance.is_staff or instance.is_superuser else "EMPLOYEE"

    employee = None

    # Only create Employee for non-admin users
    if role == "EMPLOYEE":
        employee = Employee.objects.create(
            user=instance,
            name=instance.get_full_name() or instance.username,
            joining_date=today,
            probation_end_date=today + timedelta(days=90),
        )

    # Create Profile
    Profile.objects.create(
        user=instance,
        role=role,
        employee=None if role == "ADMIN" else employee,
    )
