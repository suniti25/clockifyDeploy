from rest_framework import serializers
from .models import Profile, Employee


class EmployeeSerializer(serializers.ModelSerializer):
   
    name = serializers.CharField(source="user.get_full_name", read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "name",
            "joining_date",
            "probation_end_date",
        ]

    def to_representation(self, instance):
      
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
