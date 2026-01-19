from django.db import models
from user_app.models import Employee

class LeaveRequest(models.Model):

    LEAVE_TYPE_CHOICES = [
        ('VACATION', 'Vacation'),
        ('SICK', 'Sick'),
        ('MATERNITY', 'Maternity'),
        ('PATERNITY', 'Paternity'),
        ('BEREAVEMENT', 'Bereavement'),
        ('WFH', 'Work From Home'),
    ]

    SESSION_CHOICES = [
        ('FULL', 'Full Day'),
        ('AM', 'Morning'),
        ('PM', 'Afternoon'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='leave_requests'
    )

    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    session = models.CharField(max_length=10, choices=SESSION_CHOICES, default='FULL')
    reason = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    is_paid = models.BooleanField(default=False)
    applied_at = models.DateTimeField(auto_now_add=True)
    rejection_reason = models.TextField(blank=True, null=True)
    discord_message_id = models.CharField(
        max_length=50,
        null=True,
        blank=True
    )

    def total_days(self):
        days = (self.end_date - self.start_date).days + 1
        if self.session in ['AM', 'PM']:
            return 0.5
        return days

    def __str__(self):
        return f"{self.employee.name} - {self.leave_type}"
