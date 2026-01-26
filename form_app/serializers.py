from django.db import transaction
from rest_framework import serializers

from form_app.models import LeaveRequest
from form_app.helpers import (
    validate_leave_application_inputs,
    compute_leave_days_for_payload,
    compute_paid_status,
)


class LeaveCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = ["leave_type", "start_date", "end_date", "session", "reason"]

    def validate(self, data):
        request = self.context["request"]
        profile = getattr(request.user, "profile", None)
        employee = getattr(profile, "employee", None) if profile else None

        validate_leave_application_inputs(
            profile=profile,
            employee=employee,
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
            leave_type=data.get("leave_type"),
            session=data.get("session"),
            reason=data.get("reason"),
            instance_id=self.instance.id if self.instance else None,
        )
        return data

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        employee = request.user.profile.employee

        leave_days = compute_leave_days_for_payload(employee=employee, payload=validated_data)

        is_paid = compute_paid_status(
            employee=employee,
            leave_type=validated_data["leave_type"],
            leave_days=leave_days,
            start_date=validated_data["start_date"],
        )

        return LeaveRequest.objects.create(employee=employee, is_paid=is_paid, **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        if instance.status != "PENDING":
            raise serializers.ValidationError("Only pending leave requests can be updated.")

        for field, value in validated_data.items():
            setattr(instance, field, value)

        leave_days = instance.total_days()
        instance.is_paid = compute_paid_status(
            employee=instance.employee,
            leave_type=instance.leave_type,
            leave_days=leave_days,
            start_date=instance.start_date,
            instance_id=instance.id,
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
