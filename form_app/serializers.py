from datetime import date
from django.utils.timezone import localdate
from rest_framework import serializers

from .models import LeaveRequest
from .constants import LEAVE_LIMITS


class LeaveCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = ["leave_type", "start_date", "end_date", "session", "reason"]

    def validate(self, data):
        request = self.context["request"]
        profile = request.user.profile
        employee = profile.employee

        start_date = data.get("start_date")
        end_date = data.get("end_date")
        leave_type = data.get("leave_type")
        session = data.get("session")
        today = localdate()

        # Block admins here (single source of truth)
        if profile.role == "ADMIN":
            raise serializers.ValidationError("Admins cannot apply for leave.")

        # Date validations
        if start_date < today:
            raise serializers.ValidationError(
                "You cannot apply leave with a start date in the past."
            )

        if end_date < start_date:
            raise serializers.ValidationError("End date cannot be earlier than start date.")

        # Half-day validation
        if session in ["AM", "PM"] and start_date != end_date:
            raise serializers.ValidationError("AM/PM session can only be applied for a single day.")

        # Overlapping leave check (pending + approved)
        overlapping = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status__in=["PENDING", "APPROVED"],
        )
        if self.instance:
            overlapping = overlapping.exclude(id=self.instance.id)

        if overlapping.exists():
            raise serializers.ValidationError("You already have a leave applied for this date range.")

        # Reason required
        if leave_type in ["SICK", "WFH"] and not data.get("reason"):
            raise serializers.ValidationError("Reason is required for SICK and WFH leave.")

        return data

    def _leave_year_window(self, year: int):
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        return start, end

    def _compute_paid_status(self, employee, leave_type: str, leave_days: float, instance_id=None) -> bool:
        # probation => unpaid
        is_paid = not employee.is_on_probation()

        if not is_paid:
            return False

        # Only enforce limits for types configured in LEAVE_LIMITS
        if leave_type not in LEAVE_LIMITS:
            return True

        year = localdate().year
        start_of_year, end_of_year = self._leave_year_window(year)

        qs = LeaveRequest.objects.filter(
            employee=employee,
            leave_type=leave_type,
            status="APPROVED",
            is_paid=True,
            start_date__lte=end_of_year,
            end_date__gte=start_of_year,
        )

        if instance_id:
            qs = qs.exclude(id=instance_id)

        used = sum(lr.total_days() for lr in qs)

        if used + leave_days > LEAVE_LIMITS[leave_type]:
            return False

        return True

    def create(self, validated_data):
        request = self.context["request"]
        employee = request.user.profile.employee

        temp_leave = LeaveRequest(employee=employee, **validated_data)
        leave_days = temp_leave.total_days()

        is_paid = self._compute_paid_status(employee, validated_data["leave_type"], leave_days)

        return LeaveRequest.objects.create(employee=employee, is_paid=is_paid, **validated_data)

    def update(self, instance, validated_data):
        if instance.status != "PENDING":
            raise serializers.ValidationError("Only pending leave requests can be updated.")

        for field, value in validated_data.items():
            setattr(instance, field, value)

        leave_days = instance.total_days()
        instance.is_paid = self._compute_paid_status(
            instance.employee, instance.leave_type, leave_days, instance_id=instance.id
        )

        instance.save()
        return instance

class LeaveResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = [
            "id",
            "leave_type",
            "start_date",
            "end_date",
            "session",
            "reason",
            "status",
            "is_paid",
            "applied_at",
        ]
