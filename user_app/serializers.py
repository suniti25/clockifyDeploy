from rest_framework import serializers
from .models import Profile, Employee


class EmployeeSerializer(serializers.ModelSerializer):
    # Keep API field as "name", but source it from Employee.name
    name = serializers.CharField(source="name", read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "name",
            "joining_date",
            "probation_end_date",
        ]

    def to_representation(self, instance):
        """
        Ensure consistent name across the system:
        1) Prefer Employee.name (source of truth, populated by signals)
        2) Fallback to user full name / username if Employee.name is empty
        """
        data = super().to_representation(instance)

        if not data.get("name"):
            user = getattr(instance, "user", None)
            if user:
                full_name = (user.get_full_name() or "").strip()
                data["name"] = full_name or user.username

        return data


class ProfileSerializer(serializers.ModelSerializer):
    employee = EmployeeSerializer(allow_null=True, read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Profile
        fields = ["id", "role", "email", "employee"]
