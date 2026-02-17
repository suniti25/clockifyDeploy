from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Profile, Employee
from form_app.policies import compute_probation_end_date


@receiver(post_save, sender=User)
def ensure_profile_and_employee_exist(sender, instance, created, **kwargs):
    if not created:
        return

    role = "ADMIN" if instance.is_staff or instance.is_superuser else "EMPLOYEE"

    profile, _ = Profile.objects.get_or_create(
        user=instance,
        defaults={"role": role},
    )

    if role == "EMPLOYEE":
        joining_date = timezone.localdate()
        employee, _ = Employee.objects.get_or_create(
            user=instance,
            defaults={
                "name": instance.get_full_name() or instance.username,
                "joining_date": joining_date, 
                "probation_end_date": compute_probation_end_date(joining_date),
            },
        )

        if profile.employee_id != employee.id:
            profile.employee = employee
            profile.role = role
            profile.save(update_fields=["employee", "role"])
    else:
        if profile.role != role:
            profile.role = role
            profile.save(update_fields=["role"])
