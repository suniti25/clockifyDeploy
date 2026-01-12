from rest_framework import serializers
from .models import Profile, Employee

class EmployeeSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    
    class Meta:
        model = Employee
        fields = [
            'id',
            'name',
            'joining_date',
            'probation_end_date'
        ]
    
    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


class ProfileSerializer(serializers.ModelSerializer):
    employee = EmployeeSerializer(allow_null=True)
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Profile
        fields = ['id', 'role', 'email', 'employee']

