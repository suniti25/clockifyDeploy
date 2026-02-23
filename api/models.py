from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User


class Employee(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    joining_date = models.DateField()
    probation_end_date = models.DateField(null=True, blank=True)

    def is_on_probation(self):
        if not self.probation_end_date:
            return False
        return timezone.now().date() < self.probation_end_date

    def __str__(self):
        return self.name


class Profile(models.Model):
    ROLE_CHOICES = [
        ("EMPLOYEE", "Employee"),
        ("ADMIN", "Admin"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    employee = models.OneToOneField(
        Employee, on_delete=models.CASCADE, null=True, blank=True
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="EMPLOYEE")

    def __str__(self):
        return f"{self.user.username} - {self.role}"


class LeaveRequest(models.Model):
    LEAVE_TYPES = [
        ("VACATION", "Vacation"),
        ("SICK", "Sick"),
        ("MATERNITY", "Maternity"),
        ("PATERNITY", "Paternity"),
        ("BEREAVEMENT", "Bereavement"),
    ]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_requests"
    )
    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPES)
    start_date = models.DateField()
    end_date = models.DateField()
    is_half_day = models.BooleanField(default=False)
    reason = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.employee.name} - {self.leave_type} ({self.status})"


class LeaveBalance(models.Model):
    LEAVE_TYPES = [
        ("VACATION", "Vacation"),
        ("SICK", "Sick"),
    ]

    employee = models.OneToOneField(Employee, on_delete=models.CASCADE)
    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPES)
    total = models.IntegerField()
    used = models.IntegerField(default=0)

    @property
    def remaining(self):
        return self.total - self.used
