from rest_framework import serializers
from django.utils.timezone import localdate
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
        request = self.context['request']
        employee = request.user.profile.employee

        start_date = data.get('start_date')
        end_date = data.get('end_date')
        leave_type = data.get('leave_type')
        today = localdate()

       # Date validations 
        if start_date < today:
            raise serializers.ValidationError(
                "You cannot apply leave with a start date in the past."
            )

        if end_date < start_date:
            raise serializers.ValidationError(
                "End date cannot be earlier than start date."
            )

        if end_date < today:
            self.context['warning'] = (
                "Warning: The leave period you selected has already finished. "
                "You can still submit if you want."
            )

        # OVERLAPPING LEAVE CHECK
       
        overlapping = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status__in=['PENDING', 'APPROVED'],
        )

        # Exclude self when updating
        if self.instance:
            overlapping = overlapping.exclude(id=self.instance.id)

        if overlapping.exists():
            raise serializers.ValidationError(
                "You already have a leave applied for this date range."
            )

        # Reason required
        if leave_type in ['SICK', 'WFH'] and not data.get('reason'):
            raise serializers.ValidationError(
                "Reason is required for SICK and WFH leave."
            )

        return data
    # CREATE
    def create(self, validated_data):
        request = self.context['request']
        profile = request.user.profile

        if profile.role == 'ADMIN':
            raise serializers.ValidationError(
                "Admins cannot apply for leave."
            )

        employee = profile.employee

        # Calculate leave days
        temp_leave = LeaveRequest(**validated_data)
        leave_days = temp_leave.total_days()

        # Paid / unpaid logic
        is_paid = not employee.is_on_probation()

        if validated_data['leave_type'] in LEAVE_LIMITS and is_paid:
            year = localdate().year
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
    # Update leave request
    def update(self, instance, validated_data):
      
        if instance.status != 'PENDING':
            raise serializers.ValidationError(
                "Only pending leave requests can be updated."
            )
        # Update fields
        for field, value in validated_data.items():
            setattr(instance, field, value)
        # Recalculating paid status
        employee = instance.employee
        leave_days = instance.total_days()
        is_paid = not employee.is_on_probation()

        if instance.leave_type in LEAVE_LIMITS and is_paid:
            year = localdate().year
            used = sum(
                leave.total_days()
                for leave in LeaveRequest.objects.filter(
                    employee=employee,
                    leave_type=instance.leave_type,
                    status='APPROVED',
                    start_date__year=year,
                    is_paid=True
                ).exclude(id=instance.id)
            )

            if used + leave_days > LEAVE_LIMITS[instance.leave_type]:
                is_paid = False

        instance.is_paid = is_paid
        instance.save()
        return instance

# RESPONSE SERIALIZER

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
