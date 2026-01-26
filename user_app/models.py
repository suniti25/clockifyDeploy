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

    def is_on_probation(self) -> bool:
        """
        Returns True if today is within probation period (inclusive).
        """
        return timezone.localdate() <= self.probation_end_date

    def __str__(self) -> str:
        return self.user.get_full_name() or self.user.username


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
