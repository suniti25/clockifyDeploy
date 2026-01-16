from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from datetime import date, timedelta
from .models import Profile, Employee


@receiver(post_save, sender=User)
def create_profile_and_employee(sender, instance, created, **kwargs):
    if created:
        employee = Employee.objects.create(
            user=instance,
            name=instance.get_full_name() or instance.username,
            joining_date=date.today(),
            probation_end_date=date.today() + timedelta(days=90)
        )

        Profile.objects.create(
            user=instance,
            role='EMPLOYEE',
            employee=employee
        )
