from django.db import models
from user_app.models import Employee

class LeaveRequest(models.Model):

    LEAVE_TYPES = [
        ('VACATION', 'Vacation'),
        ('SICK', 'Sick'),
        ('MATERNITY', 'Maternity'),
        ('PATERNITY', 'Paternity'),
        ('BEREAVEMENT', 'Bereavement'),
        ('WFH', 'Work From Home'),
    ]

    SESSION_TYPES = [
        ('FULL', 'Full Day'),
        ('AM', 'Half Day AM'),
        ('PM', 'Half Day PM'),
    ]

    STATUS = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)

    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPES)
    start_date = models.DateField()
    end_date = models.DateField()
    session = models.CharField(max_length=10, choices=SESSION_TYPES)
    reason = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=STATUS, default='PENDING')
    is_paid = models.BooleanField(default=True)

    applied_at = models.DateTimeField(auto_now_add=True)

    def total_days(self):
        days = (self.end_date - self.start_date).days + 1
        if self.session in ['AM', 'PM']:
            return 0.5
        return days
