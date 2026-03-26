from django.db import transaction
from rest_framework import serializers

from form_app.models import LeaveRequest, Holiday
from form_app.helpers import (
    validate_leave_application_inputs,
    compute_leave_days_for_payload,
    compute_paid_status,
    display_is_paid,
)


class LeaveCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = ["leave_type", "start_date", "end_date", "session", "reason"]
        extra_kwargs = {"session": {"required": True}}

    def _get_employee(self):
        emp = self.context.get("employee")
        if emp:
            return emp

        request = self.context.get("request")
        profile = getattr(request.user, "profile", None)
        return getattr(profile, "employee", None) if profile else None

    def validate(self, data):
        request = self.context.get("request")
        profile = getattr(request.user, "profile", None)
        employee = self._get_employee()

        allow_overlap_with_id = self.context.get("allow_overlap_with_id")

        normalized_session, normalized_leave_type = validate_leave_application_inputs(
            profile=profile,
            employee=employee,
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
            leave_type=data.get("leave_type"),
            session=data.get("session"),
            reason=data.get("reason") or "",
            instance_id=self.instance.id if self.instance else None,
            allow_overlap_with_id=allow_overlap_with_id,
        )

        data["leave_type"] = normalized_leave_type
        data["session"] = normalized_session

        if data.get("reason") is None:
            data["reason"] = ""

        return data

    @transaction.atomic
    def create(self, validated_data):
        employee = self._get_employee()
        if not employee:
            raise serializers.ValidationError("Employee record not found.")

        #  keep DB fields consistent
        validated_data["start_session"] = validated_data["session"]
        validated_data["end_session"] = validated_data["session"]

        leave_days = compute_leave_days_for_payload(
            employee=employee, payload=validated_data
        )

        is_paid = compute_paid_status(
            employee=employee,
            leave_type=validated_data["leave_type"],
            leave_days=leave_days,
            start_date=validated_data["start_date"],
            end_date=validated_data["end_date"],
        )

        return LeaveRequest.objects.create(
            employee=employee,
            is_paid=bool(is_paid),
            **validated_data,
        )

    @transaction.atomic
    def update(self, instance, validated_data):
        if instance.status != "PENDING":
            raise serializers.ValidationError(
                "Only pending leave requests can be updated."
            )

        for field, value in validated_data.items():
            setattr(instance, field, value)

        #  sync boundary fields
        instance.start_session = instance.session
        instance.end_session = instance.session

        leave_days = float(instance.total_days())

        instance.is_paid = bool(
            compute_paid_status(
                employee=instance.employee,
                leave_type=instance.leave_type,
                leave_days=leave_days,
                start_date=instance.start_date,
                end_date=instance.end_date,
                instance_id=instance.id,
            )
        )
        instance.has_reapplied = True
        instance.save()
        return instance


class LeaveResponseSerializer(serializers.ModelSerializer):
    is_paid = serializers.SerializerMethodField()

    def get_is_paid(self, obj):
        return display_is_paid(obj.leave_type, getattr(obj, "is_paid", None))

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


class LeaveUpdateByBodySerializer(serializers.Serializer):
    form_id = serializers.IntegerField()
    form_payload = serializers.DictField()

    def validate_form_payload(self, payload):
        allowed = {"leave_type", "start_date", "end_date", "session", "reason"}
        unknown = set(payload.keys()) - allowed
        if unknown:
            raise serializers.ValidationError(
                f"Unknown fields: {sorted(list(unknown))}"
            )
        return payload


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = ["id", "date", "name", "description", "is_active", "updated_at"]
