from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from user_app.models import Profile, Employee
from form_app.policies import (
    compute_probation_end_date,
    get_leave_year_range_for_employee,
)

User = get_user_model()


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)
    email = serializers.EmailField(required=False)

    def validate(self, data):
        username = (data.get("username") or "").strip()
        password = data.get("password")

        try:
            matched_user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            matched_user = None
        except MultipleObjectsReturned:
            raise serializers.ValidationError(
                "Multiple accounts match this username. Please contact an administrator."
            )

        if not matched_user:
            raise serializers.ValidationError("Invalid username or password")

        # remains unchanged while login becomes case-insensitive.
        user = authenticate(username=matched_user.username, password=password)

        if not user:
            raise serializers.ValidationError("Invalid username or password")

        provided_email = data.get("email")
        if not user.email:
            if not provided_email:
                raise serializers.ValidationError(
                    {"email": "Email is required for login to send notifications."}
                )

            if User.objects.filter(email=provided_email).exclude(pk=user.pk).exists():
                raise serializers.ValidationError(
                    {"email": "This email is already in use."}
                )

            user.email = provided_email
            user.save(update_fields=["email"])

        data["user"] = user
        return data


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)

    joining_date = serializers.DateField(required=False)

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "password_confirm",
            "joining_date",
        ]

    def validate(self, data):
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError({"password": "Passwords do not match"})

        # Require at least one symbol character (e.g. @, #, !)
        import re

        if not re.search(r"[^A-Za-z0-9]", data["password"] or ""):
            raise serializers.ValidationError(
                {"password": "Password must contain at least 1 symbol (e.g. @, #, !)."}
            )

        if User.objects.filter(
            username__iexact=(data["username"] or "").strip()
        ).exists():
            raise serializers.ValidationError({"username": "Username already exists"})

        if User.objects.filter(email=data["email"]).exists():
            raise serializers.ValidationError({"email": "Email already registered"})

        today = timezone.localdate()
        joining_date = data.get("joining_date") or today

        if joining_date > today:
            raise serializers.ValidationError(
                {"joining_date": "Joining date cannot be in the future"}
            )

        data["joining_date"] = joining_date

        return data

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        joining_date = validated_data.pop("joining_date")

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            password=password,
        )

        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"role": "EMPLOYEE"},
        )

        #  create employee if not exists
        probation_end_date = compute_probation_end_date(joining_date)
        employee, _ = Employee.objects.get_or_create(
            user=user,
            defaults={
                "joining_date": joining_date,
                "probation_end_date": probation_end_date,
            },
        )
        updates = []
        if employee.joining_date != joining_date:
            employee.joining_date = joining_date
            updates.append("joining_date")

        if employee.probation_end_date != probation_end_date:
            employee.probation_end_date = probation_end_date
            updates.append("probation_end_date")

        if updates:
            employee.save(update_fields=updates)

        today = timezone.localdate()
        leave_year_start, _ = get_leave_year_range_for_employee(employee, on_date=today)
        if joining_date < leave_year_start and not employee.reset_leave_balance:
            employee.reset_leave_balance = True
            employee.save(update_fields=["reset_leave_balance"])

        if getattr(profile, "employee_id", None) != employee.id:
            profile.employee = employee
            profile.save(update_fields=["employee"])

        return user


class UpdateEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        user = self.context.get("user")
        if User.objects.filter(email=value).exclude(pk=user.pk).exists():
            raise serializers.ValidationError("This email is already in use.")
        return value

    def save(self, **kwargs):
        user = self.context.get("user")
        user.email = self.validated_data["email"]
        user.save(update_fields=["email"])
        return user


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField()
    newPassword = serializers.CharField(write_only=True)
    newPasswordConfirm = serializers.CharField(write_only=True)

    def validate(self, data):
        pwd = (data.get("newPassword") or "").strip()
        cpwd = (data.get("newPasswordConfirm") or "").strip()
        if not pwd or not cpwd:
            raise serializers.ValidationError("Password is required")
        if pwd != cpwd:
            raise serializers.ValidationError(
                {"newPasswordConfirm": "Passwords do not match"}
            )

        validate_password(pwd)
        return data


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, data):
        user = self.context.get("user")
        if not user:
            raise serializers.ValidationError("User context is required")

        old_pwd = (data.get("old_password") or "").strip()
        new_pwd = (data.get("new_password") or "").strip()
        cpwd = (data.get("confirm_password") or "").strip()

        if not old_pwd:
            raise serializers.ValidationError(
                {"old_password": "Old password is required"}
            )

        if not user.check_password(old_pwd):
            raise serializers.ValidationError(
                {"old_password": "Old password is incorrect"}
            )

        if not new_pwd or not cpwd:
            raise serializers.ValidationError(
                "New password and confirm password are required"
            )

        if new_pwd != cpwd:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match"}
            )

        validate_password(new_pwd, user=user)
        return data

    def save(self, **kwargs):
        user = self.context["user"]
        new_pwd = (self.validated_data.get("new_password") or "").strip()
        user.set_password(new_pwd)
        user.save(update_fields=["password"])
        return user
