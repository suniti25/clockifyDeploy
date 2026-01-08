from rest_framework import serializers
from datetime import date
from .models import LeaveRequest
from .constants import LEAVE_LIMITS


class LeaveCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = LeaveRequest
        fields = [
            'leave_type',
            'start_date',
            'end_date',
            'session',
            'reason',
        ]

    def validate(self, data):
        if data['start_date'] > data['end_date']:
            raise serializers.ValidationError("End date must be after start date")

        if data['leave_type'] in ['SICK', 'WFH'] and not data.get('reason'):
            raise serializers.ValidationError("Reason is required")

        return data

    def create(self, validated_data):
        request = self.context['request']
        employee = request.user.profile.employee

        leave_days = LeaveRequest(
            start_date=validated_data['start_date'],
            end_date=validated_data['end_date'],
            session=validated_data.get('session', 'FULL')
        ).total_days()

        # Probation check
        is_paid = not employee.is_on_probation()

        # Leave limit check
        if validated_data['leave_type'] in LEAVE_LIMITS and is_paid:
            year = date.today().year
            used = sum(
                leave.total_days()
                for leave in LeaveRequest.objects.filter(
                    employee=employee,
                    leave_type=validated_data['leave_type'],
                    status='APPROVED',
                    start_date__year=year,
                    is_paid=True
                )
            )

            if used + leave_days > LEAVE_LIMITS[validated_data['leave_type']]:
                is_paid = False

        return LeaveRequest.objects.create(
            employee=employee,
            is_paid=is_paid,
            **validated_data
        )


class LeaveResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = [
            'id',
            'leave_type',
            'start_date',
            'end_date',
            'session',
            'reason',
            'status',
            'is_paid',
            'applied_at',
        ]
