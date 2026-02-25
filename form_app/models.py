from django.db import models
from django.core.exceptions import ValidationError
from user_app.models import Employee


class LeaveRequest(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_VOIDED = "VOIDED"

    LEAVE_TYPE_CHOICES = [
        ("VACATION", "Vacation"),
        ("SICK", "Sick"),
        ("MATERNITY", "Maternity"),
        ("PATERNITY", "Paternity"),
        ("BEREAVEMENT", "Bereavement"),
        ("WFH", "Work From Home"),
    ]

    SESSION_CHOICES = [
        ("FULL", "Full Day"),
        ("AM", "Morning"),
        ("PM", "Afternoon"),
    ]

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_VOIDED, "Voided"),
    ]

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="leave_requests",
    )

    # Link to original request if this is a reapply
    reapplied_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reapplications",
    )

    has_reapplied = models.BooleanField(default=False)

    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()

    # Boundary sessions
    start_session = models.CharField(
        max_length=10, choices=SESSION_CHOICES, default="FULL"
    )
    end_session = models.CharField(
        max_length=10, choices=SESSION_CHOICES, default="FULL"
    )

    # Single session for each day
    session = models.CharField(max_length=10, choices=SESSION_CHOICES, default="FULL")

    reason = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    is_paid = models.BooleanField(default=False)

    applied_at = models.DateTimeField(auto_now_add=True)

    # track when approval happened (needed for “approved today” cron)
    approved_at = models.DateTimeField(null=True, blank=True)

    rejection_reason = models.TextField(blank=True, null=True)
    approval_reason = models.TextField(blank=True, null=True)
    discord_message_id = models.CharField(max_length=50, null=True, blank=True)

    # Google Calendar sync (event id stored after creation)
    google_event_id = models.TextField(blank=True, default="")

    #  prevents duplicate daily public messages per leave
    notified_employee_at = models.DateTimeField(null=True, blank=True)
    notified_admin_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("start_date cannot be after end_date")

        # For single-day leave, start_session must equal end_session
        if self.start_date and self.end_date and self.start_date == self.end_date:
            if self.start_session != self.end_session:
                raise ValidationError(
                    "For single-day leave, start_session must equal end_session."
                )

    def total_days(self) -> float:
        import datetime

        if not self.start_date or not self.end_date:
            return 0.0

        # Generate all dates in the range
        day_count = (self.end_date - self.start_date).days + 1
        if day_count <= 0:
            return 0.0

        days_list = [
            self.start_date + datetime.timedelta(days=i) for i in range(day_count)
        ]
        # Exclude weekends
        weekdays = [d for d in days_list if d.weekday() < 5]  # 0=Mon, 6=Sun
        num_days = len(weekdays)

        session = (self.session or "FULL").strip().upper()
        if session == "FD":
            session = "FULL"

        if session in ("AM", "PM"):
            return max(float(num_days) * 0.5, 0.0)

        if self.start_date == self.end_date:
            # If it's a weekend, return 0
            if self.start_date.weekday() >= 5:
                return 0.0
            return 1.0

        days = float(num_days)
        start_sess = (self.start_session or "FULL").strip().upper()
        end_sess = (self.end_session or "FULL").strip().upper()
        if start_sess == "FD":
            start_sess = "FULL"
        if end_sess == "FD":
            end_sess = "FULL"

        # Adjust for half-day sessions only if the start/end day is a weekday
        if start_sess == "PM" and self.start_date.weekday() < 5:
            days -= 0.5
        if end_sess == "AM" and self.end_date.weekday() < 5:
            days -= 0.5

        return max(days, 0.0)

    def __str__(self):
        name = getattr(self.employee, "name", "Unknown")
        return f"{name} - {self.leave_type}"


class LeavePolicySettings(models.Model):
    global_renewal_date = models.DateField(null=True, blank=True)

    carryover_percentage = models.IntegerField(default=50)
    probation_period_days = models.IntegerField(default=90)

    vacation_days = models.IntegerField(default=14)
    sick_days = models.IntegerField(default=12)
    maternity_days = models.IntegerField(default=60)
    paternity_days = models.IntegerField(default=10)
    bereavement_days = models.IntegerField(default=3)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "LeavePolicySettings"
