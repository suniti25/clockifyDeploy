from django.db import models
from django.contrib.auth.models import User
from datetime import date
from django.utils import timezone


class Employee(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="employee"
    )
    name = models.CharField(max_length=255, blank=True, null=True)

    joining_date = models.DateField()
    probation_end_date = models.DateField()

    def is_on_probation(self):
        return timezone.localdate() <= self.probation_end_date

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Profile(models.Model):
    ROLE_CHOICES = [
        ('EMPLOYEE', 'Employee'),
        ('ADMIN', 'Admin'),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='EMPLOYEE'
    )
    employee = models.OneToOneField(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.user.username} ({self.role})"
