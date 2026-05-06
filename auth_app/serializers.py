from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from collections.abc import Mapping

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
    role = serializers.ChoiceField(
        choices=[Profile.ROLE_EMPLOYEE, Profile.ROLE_MANAGER],
        required=False,
        default=Profile.ROLE_EMPLOYEE,
    )

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
            "role",
            "joining_date",
        ]

    def validate(self, data):
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError({"password": ["Passwords do not match"]})

        try:
            validate_password((data.get("password") or "").strip())
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})

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
        role = validated_data.pop("role", Profile.ROLE_EMPLOYEE)
        joining_date = validated_data.pop("joining_date")

        first_name = (validated_data.get("first_name") or "").strip()
        last_name = (validated_data.get("last_name") or "").strip()
        display_name = f"{first_name} {last_name}".strip()

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            first_name=first_name,
            last_name=last_name,
            password=password,
        )

        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"role": role},
        )

        #  create employee if not exists
        probation_end_date = compute_probation_end_date(joining_date)
        employee, _ = Employee.objects.get_or_create(
            user=user,
            defaults={
                "name": display_name or user.username,
                "joining_date": joining_date,
                "probation_end_date": probation_end_date,
            },
        )
        updates = []
        if not (employee.name or "").strip():
            employee.name = display_name or user.username
            updates.append("name")

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

        profile_updates = []
        if profile.role != role:
            profile.role = role
            profile_updates.append("role")

        if getattr(profile, "employee_id", None) != employee.id:
            profile.employee = employee
            profile_updates.append("employee")

        if profile_updates:
            profile.save(update_fields=profile_updates)

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
            errors = {}
            if not pwd:
                errors["newPassword"] = ["Password is required"]
            if not cpwd:
                errors["newPasswordConfirm"] = ["Password is required"]
            raise serializers.ValidationError(errors)
        if pwd != cpwd:
            raise serializers.ValidationError(
                {"newPasswordConfirm": ["Passwords do not match"]}
            )

        try:
            validate_password(pwd)
        except DjangoValidationError as exc:
            # Normalize Django's password validator errors into DRF field errors.
            raise serializers.ValidationError({"newPassword": list(exc.messages)})
        return data


class ChangePasswordSerializer(serializers.Serializer):
    # Accept both snake_case and camelCase keys to keep the API compatible
    # with different frontend conventions.
    old_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    oldPassword = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    new_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    newPassword = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    confirm_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    confirmPassword = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    newPasswordConfirm = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    def _prefers_camelcase_errors(self) -> bool:
        initial = getattr(self, "initial_data", None) or {}
        if not isinstance(initial, Mapping):
            return False
        return any(
            k in initial
            for k in (
                "oldPassword",
                "newPassword",
                "confirmPassword",
                "newPasswordConfirm",
            )
        )

    def _pick_error_key(self, snake_key: str, camel_key: str) -> str:
        initial = getattr(self, "initial_data", None) or {}
        if isinstance(initial, Mapping):
            has_snake = snake_key in initial
            has_camel = camel_key in initial
            if has_camel and not has_snake:
                return camel_key
            if has_snake and not has_camel:
                return snake_key
        return camel_key if self._prefers_camelcase_errors() else snake_key

    def _pick_confirm_error_key(self) -> str:
        initial = getattr(self, "initial_data", None) or {}
        if isinstance(initial, Mapping):
            # Prefer whichever confirm key the client actually sent.
            if "newPasswordConfirm" in initial and "confirm_password" not in initial:
                return "newPasswordConfirm"
            if "confirmPassword" in initial and "confirm_password" not in initial:
                return "confirmPassword"
            if "confirm_password" in initial and "confirmPassword" not in initial:
                return "confirm_password"
        return (
            "confirmPassword"
            if self._prefers_camelcase_errors()
            else "confirm_password"
        )

    @staticmethod
    def _dedupe_messages(messages):
        if not isinstance(messages, list):
            return messages
        seen = set()
        unique = []
        for msg in messages:
            key = str(msg)
            if key in seen:
                continue
            seen.add(key)
            unique.append(msg)
        return unique

    def validate(self, data):
        user = self.context.get("user")
        if not user:
            raise serializers.ValidationError("User context is required")

        old_key = self._pick_error_key("old_password", "oldPassword")
        new_key = self._pick_error_key("new_password", "newPassword")
        confirm_key = self._pick_confirm_error_key()

        old_pwd = (data.get("old_password") or data.get("oldPassword") or "").strip()
        new_pwd = (data.get("new_password") or data.get("newPassword") or "").strip()
        cpwd = (
            data.get("confirm_password")
            or data.get("confirmPassword")
            or data.get("newPasswordConfirm")
            or ""
        ).strip()

        # Normalize to canonical keys so `save()` can rely on them.
        data["old_password"] = old_pwd
        data["new_password"] = new_pwd
        data["confirm_password"] = cpwd

        if not old_pwd:
            # Return both snake_case and camelCase keys so different frontends
            # can map field errors consistently.
            raise serializers.ValidationError(
                {
                    old_key: ["Old password is required"],
                }
            )

        if not user.check_password(old_pwd):
            raise serializers.ValidationError(
                {
                    old_key: ["Old password is incorrect"],
                }
            )

        if not new_pwd or not cpwd:
            errors = {}
            if not new_pwd:
                errors[new_key] = ["New password is required"]
            if not cpwd:
                errors[confirm_key] = ["Confirm password is required"]
            raise serializers.ValidationError(errors)

        if new_pwd != cpwd:
            raise serializers.ValidationError(
                {
                    confirm_key: ["Passwords do not match"],
                }
            )

        # Prevent reusing the existing password.
        if user.check_password(new_pwd):
            msg = "New password must be different from the old password"
            raise serializers.ValidationError({new_key: [msg]})

        try:
            validate_password(new_pwd, user=user)
        except DjangoValidationError as exc:
            messages = self._dedupe_messages(list(exc.messages))
            raise serializers.ValidationError({new_key: messages})
        return data

    def save(self, **kwargs):
        user = self.context["user"]
        new_pwd = (
            self.validated_data.get("new_password")
            or self.validated_data.get("newPassword")
            or ""
        ).strip()
        user.set_password(new_pwd)
        user.save(update_fields=["password"])
        return user


class RoleFromTokensSerializer(serializers.Serializer):
    # Optional because access is usually in Authorization header and refresh in an HTTP-only cookie.
    # The view accepts either token from header/cookie/body.
    access = serializers.CharField(required=False, allow_blank=True)
    refresh = serializers.CharField(required=False, allow_blank=True)
