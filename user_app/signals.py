from datetime import date, timedelta

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, Employee


@receiver(post_save, sender=User)
def create_profile_and_employee(sender, instance, created, **kwargs):
    if not created:
        return

    # Determine role correctly
    role = "ADMIN" if instance.is_staff or instance.is_superuser else "EMPLOYEE"

    employee = None

    # Only create Employee for non-admin users
    if role == "EMPLOYEE":
        employee = Employee.objects.create(
            user=instance,
            name=instance.get_full_name() or instance.username,
            joining_date=date.today(),
            probation_end_date=date.today() + timedelta(days=90),
        )

    # Create Profile
    Profile.objects.create(
        user=instance,
        role=role,
        employee=None if role == "ADMIN" else employee,
    )
