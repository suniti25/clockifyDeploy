from rest_framework import serializers
from user_app.models import Employee, Profile


class EmployeeDetailSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    is_active = serializers.BooleanField(source='user.is_active', read_only=True)
    date_joined = serializers.DateTimeField(source='user.date_joined', read_only=True)
    is_on_probation = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'name',
            'joining_date',
            'probation_end_date',
            'is_on_probation',
            'is_active',
            'date_joined'
        ]

    def get_is_on_probation(self, obj):
        return obj.is_on_probation()


class AllUsersDetailSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    is_active = serializers.BooleanField()
    date_joined = serializers.DateTimeField()
    profile = serializers.SerializerMethodField()
    employee = serializers.SerializerMethodField()

    def get_profile(self, obj):
        try:
            profile = obj.profile
            return {
                'role': profile.role,
            }
        except Profile.DoesNotExist:
            return None

    def get_employee(self, obj):
        try:
            employee = obj.employee
            return employee.id
        except Employee.DoesNotExist:
            return None
