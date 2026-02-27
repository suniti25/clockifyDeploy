from __future__ import annotations

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Employee(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="employee",
    )
    name = models.CharField(max_length=255, blank=True, null=True)

    joining_date = models.DateField()
    probation_end_date = models.DateField()

    current_project = models.CharField(max_length=255, blank=True, null=True)

    leave_renewal_date_override = models.DateField(null=True, blank=True)

    leave_renewal_override_set_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="renewal_override_changes",
    )
    leave_renewal_override_set_at = models.DateTimeField(null=True, blank=True)

    # Per-employee leave limit overrides,

    leave_limits_override = models.JSONField(blank=True, null=True)

    reset_leave_balance = models.BooleanField(default=False)

    def is_on_probation(self, on_date=None) -> bool:

        if on_date is None:
            on_date = timezone.localdate()

        try:
            from form_app.policies import get_effective_probation_end_date

            probation_end = get_effective_probation_end_date(self)
        except Exception:
            probation_end = getattr(self, "probation_end_date", None)

        joining_date = getattr(self, "joining_date", None)
        if not joining_date or not probation_end:
            return False
        return joining_date <= on_date <= probation_end

    def __str__(self) -> str:
        return self.user.get_full_name() or self.user.username


class Project(models.Model):
    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Profile(models.Model):
    ROLE_EMPLOYEE = "EMPLOYEE"
    ROLE_ADMIN = "ADMIN"

    ROLE_CHOICES = [
        (ROLE_EMPLOYEE, "Employee"),
        (ROLE_ADMIN, "Admin"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_EMPLOYEE,
    )

    employee = models.OneToOneField(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="profile",
    )

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role})"
