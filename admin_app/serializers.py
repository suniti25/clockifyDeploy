from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from form_app.models import LeaveRequest, LeavePolicySettings
from form_app.helpers import display_is_paid
from form_app.constants import LEAVE_LIMITS
from form_app.policies import (
    compute_probation_end_date,
    get_leave_year_range_for_employee,
)
from user_app.models import Employee, Profile
from user_app.models import Project

from .helpers import (
    get_employee_leaves,
    total_leave_this_year,
    used_leaves_by_type,
    remaining_leaves,
)


class LeaveMiniSerializer(serializers.ModelSerializer):
    is_paid = serializers.SerializerMethodField()

    def get_is_paid(self, obj):
        return display_is_paid(obj.leave_type, getattr(obj, "is_paid", None))

    class Meta:
        model = LeaveRequest
        fields = [
            "id",
            "status",
            "leave_type",
            "start_date",
            "end_date",
            "session",
            "applied_at",
            "reason",
            "is_paid",
            "rejection_reason",
        ]


class EmployeeDetailSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    date_joined = serializers.DateTimeField(source="user.date_joined", read_only=True)
    is_on_probation = serializers.SerializerMethodField()
    probation_period_days = serializers.SerializerMethodField()

    # Used as an annual anchor (month/day) for computing leave-year windows.
    leave_renewal_date_override = serializers.DateField(read_only=True)

    class Meta:
        model = Employee
        fields = [
            "id",
            "user_id",
            "username",
            "email",
            "first_name",
            "last_name",
            "name",
            "joining_date",
            "probation_end_date",
            "probation_period_days",
            "leave_renewal_date_override",
            "current_project",
            "is_on_probation",
            "is_active",
            "date_joined",
        ]

    def get_is_on_probation(self, obj):
        fn = getattr(obj, "is_on_probation", None)
        return bool(fn()) if callable(fn) else False

    def get_probation_period_days(self, obj):
        try:
            if obj.joining_date and obj.probation_end_date:
                return (obj.probation_end_date - obj.joining_date).days
        except Exception:
            return None
        return None


class AdminEmployeeUpdateSerializer(serializers.Serializer):
    employee_id = serializers.IntegerField()

    username = serializers.CharField(required=False)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    confirm_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    current_project = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    project_id = serializers.IntegerField(required=False)
    joining_date = serializers.DateField(required=False)

    # Some frontends send this as a toggle. If false, treat as "probation is off".
    # If true, probation end date will be (re)computed from joining_date unless
    # explicitly provided.
    is_on_probation = serializers.BooleanField(required=False)

    # Optional per-employee renewal anchor override (month/day are used).
    leave_renewal_date_override = serializers.DateField(required=False, allow_null=True)
    # Compatibility for some frontends that send camelCase.
    leaveRenewalDateOverride = serializers.DateField(
        required=False, allow_null=True, write_only=True
    )

    # probation can be supplied either as a concrete end date or as days from joining date
    probation_end_date = serializers.DateField(required=False)
    probation_period_days = serializers.IntegerField(required=False, min_value=0)

    # Optional per-employee leave limit overrides, e.g. {"VACATION": 18}
    # Use null to clear overrides.
    leave_limits_override = serializers.JSONField(required=False, allow_null=True)
    # Compatibility for some frontends that send camelCase.
    leaveLimitsOverride = serializers.JSONField(
        required=False, allow_null=True, write_only=True
    )

    # Some UIs display (and resubmit) *remaining days left* in the same fields.
    # If the client sends the current remaining values unchanged, we should not
    # treat it as an intent to overwrite yearly limits.
    # Set this to true to force applying leave_limits_override as yearly limits.
    apply_leave_limits_override = serializers.BooleanField(
        required=False, default=False, write_only=True
    )

    # Optional per-employee manual used leave adjustments for the current leave year,
    # e.g. {"VACATION": 5, "SICK": 1.5}. Stored under a reserved key in
    # Employee.leave_limits_override.
    manual_used_by_type = serializers.JSONField(required=False, allow_null=True)
    # Compatibility for some frontends that send camelCase.
    manualUsedByType = serializers.JSONField(
        required=False, allow_null=True, write_only=True
    )

    def validate(self, data: dict[str, Any]):
        employee_id = data["employee_id"]

        emp = Employee.objects.select_related("user").filter(id=employee_id).first()
        if not emp:
            raise serializers.ValidationError({"employee_id": "Employee not found."})

        pwd = (data.get("password") or "").strip()
        cpwd = (data.get("confirm_password") or "").strip()
        if pwd or cpwd:
            if pwd != cpwd:
                raise serializers.ValidationError(
                    {"confirm_password": "Passwords do not match."}
                )
            if len(pwd) < 6:
                raise serializers.ValidationError(
                    {"password": "Password must be at least 6 characters."}
                )

        if "username" in data:
            username = (data.get("username") or "").strip()
            if not username:
                raise serializers.ValidationError(
                    {"username": "Username cannot be empty."}
                )
            # Ensure uniqueness against all other users
            if (
                User.objects.filter(username__iexact=username)
                .exclude(id=emp.user_id)
                .exists()
            ):
                raise serializers.ValidationError(
                    {"username": "Username already exists."}
                )
            data["username"] = username

        joining_date = data.get("joining_date")
        if joining_date and joining_date > timezone.localdate():
            raise serializers.ValidationError(
                {"joining_date": "Joining date cannot be in the future."}
            )

        if "probation_end_date" in data and "probation_period_days" in data:
            raise serializers.ValidationError(
                {
                    "probation_period_days": "Provide either probation_end_date or probation_period_days, not both."
                }
            )

        if "project_id" in data and "current_project" in data:
            raise serializers.ValidationError(
                {
                    "project_id": "Provide either project_id or current_project, not both."
                }
            )

        if "project_id" in data:
            project_id = data.get("project_id")
            if project_id is None:
                raise serializers.ValidationError(
                    {"project_id": "project_id cannot be null."}
                )
            if not Project.objects.filter(id=project_id, is_active=True).exists():
                raise serializers.ValidationError(
                    {"project_id": "Project not found or inactive."}
                )

        # Basic sanity if end date provided
        probation_end_date = data.get("probation_end_date")
        # If joining_date is not being updated, we'll validate against existing joining_date during update
        if joining_date is not None and probation_end_date is not None:
            if probation_end_date < joining_date:
                raise serializers.ValidationError(
                    {
                        "probation_end_date": "Probation end date cannot be before joining date."
                    }
                )

        # Normalize optional leave limits override (case-insensitive keys)
        if "leave_limits_override" not in data and "leaveLimitsOverride" in data:
            data["leave_limits_override"] = data.get("leaveLimitsOverride")

        # Normalize renewal override (camelCase)
        if (
            "leave_renewal_date_override" not in data
            and "leaveRenewalDateOverride" in data
        ):
            data["leave_renewal_date_override"] = data.get("leaveRenewalDateOverride")

        if "leave_limits_override" in data:
            overrides = data.get("leave_limits_override")
            if overrides is None:
                # explicit clear
                pass
            elif not isinstance(overrides, dict):
                raise serializers.ValidationError(
                    {
                        "leave_limits_override": "Must be an object mapping leave types to numbers."
                    }
                )
            else:
                allowed = set(str(k).strip().upper() for k in LEAVE_LIMITS.keys())
                cleaned: dict[str, float] = {}
                for raw_key, raw_val in overrides.items():
                    if not isinstance(raw_key, str):
                        raise serializers.ValidationError(
                            {"leave_limits_override": "All keys must be strings."}
                        )

                    key = raw_key.strip()
                    if not key:
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": "Leave type keys cannot be empty."
                            }
                        )

                    key_norm = key.upper()
                    if key_norm not in allowed:
                        raise serializers.ValidationError(
                            {"leave_limits_override": f"Invalid leave type '{key}'."}
                        )

                    try:
                        num = float(raw_val)
                    except (TypeError, ValueError):
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": f"Value for '{key}' must be a number."
                            }
                        )

                    if num < 0:
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": f"Value for '{key}' cannot be negative."
                            }
                        )

                    cleaned[key_norm] = num

                data["leave_limits_override"] = cleaned

        # Normalize optional manual used adjustments (case-insensitive keys)
        if "manual_used_by_type" not in data and "manualUsedByType" in data:
            data["manual_used_by_type"] = data.get("manualUsedByType")

        if "manual_used_by_type" in data:
            used_map = data.get("manual_used_by_type")
            if used_map is None:
                pass
            elif not isinstance(used_map, dict):
                raise serializers.ValidationError(
                    {
                        "manual_used_by_type": "Must be an object mapping leave types to numbers."
                    }
                )
            else:
                allowed = set(str(k).strip().upper() for k in LEAVE_LIMITS.keys())
                cleaned_used: dict[str, float] = {}
                for raw_key, raw_val in used_map.items():
                    if not isinstance(raw_key, str):
                        raise serializers.ValidationError(
                            {"manual_used_by_type": "All keys must be strings."}
                        )

                    key = raw_key.strip()
                    if not key:
                        raise serializers.ValidationError(
                            {"manual_used_by_type": "Leave type keys cannot be empty."}
                        )

                    key_norm = key.upper()
                    if key_norm not in allowed:
                        raise serializers.ValidationError(
                            {"manual_used_by_type": f"Invalid leave type '{key}'."}
                        )

                    try:
                        num = float(raw_val)
                    except (TypeError, ValueError):
                        raise serializers.ValidationError(
                            {
                                "manual_used_by_type": f"Value for '{key}' must be a number."
                            }
                        )

                    if num < 0:
                        raise serializers.ValidationError(
                            {
                                "manual_used_by_type": f"Value for '{key}' cannot be negative."
                            }
                        )

                    cleaned_used[key_norm] = num

                data["manual_used_by_type"] = cleaned_used

        return data

    @transaction.atomic
    def update_employee(self):
        data = self.validated_data
        emp = (
            Employee.objects.select_related("user")
            .select_for_update()
            .get(id=data["employee_id"])
        )
        user = emp.user

        # Update user fields
        for f in ("username", "first_name", "last_name"):
            if f in data:
                setattr(user, f, data[f])

        pwd = (data.get("password") or "").strip()
        if pwd:
            user.set_password(pwd)

        user.save()

        updates = []

        if "project_id" in data:
            proj = Project.objects.get(id=data["project_id"])
            emp.current_project = proj.name
            updates.append("current_project")

        if "current_project" in data:
            emp.current_project = data.get("current_project") or ""
            updates.append("current_project")

        joining_date = data.get("joining_date", None)
        if joining_date is not None and emp.joining_date != joining_date:
            emp.joining_date = joining_date
            updates.append("joining_date")

        # If probation is turned off, anchor renewal to joining_date by setting
        # probation_end_date to the day before joining_date.
        prob_toggle = (
            data.get("is_on_probation", None) if "is_on_probation" in data else None
        )
        if prob_toggle is False:
            emp.probation_end_date = emp.joining_date - timedelta(days=1)
            updates.append("probation_end_date")
        elif prob_toggle is True:
            # If re-enabling probation and admin didn't explicitly set a probation end,
            # compute it from joining_date using policy settings.
            if "probation_end_date" not in data and "probation_period_days" not in data:
                emp.probation_end_date = compute_probation_end_date(emp.joining_date)
                updates.append("probation_end_date")

        if "leave_renewal_date_override" in data:
            # This is used as an annual anchor (month/day), so past dates are valid.
            emp.leave_renewal_date_override = data.get("leave_renewal_date_override")
            updates.append("leave_renewal_date_override")

        if "probation_end_date" in data:
            ped = data.get("probation_end_date")
            # Validate against final joining_date (updated or existing)
            if ped is not None and ped < emp.joining_date:
                raise serializers.ValidationError(
                    {
                        "probation_end_date": "Probation end date cannot be before joining date."
                    }
                )
            emp.probation_end_date = ped
            updates.append("probation_end_date")

        if "probation_period_days" in data:
            days = data.get("probation_period_days")
            # compute end date from current joining date
            d = int(days)
            emp.probation_end_date = (
                emp.joining_date - timedelta(days=1)
                if d <= 0
                else emp.joining_date + timedelta(days=d - 1)
            )
            updates.append("probation_end_date")

        if "leave_limits_override" in data:
            overrides = data.get("leave_limits_override")

            force_apply = bool(data.get("apply_leave_limits_override", False))
            if overrides is not None and not isinstance(overrides, dict):
                # Should be prevented by validate(), but keep it safe.
                raise serializers.ValidationError(
                    {
                        "leave_limits_override": "Must be an object mapping leave types to numbers."
                    }
                )

            if overrides is not None and not force_apply:
                if not overrides:
                    overrides = None  # empty object
                else:
                    try:
                        current_remaining = remaining_leaves(emp)
                    except Exception:
                        current_remaining = {}

                    def _eq(a, b) -> bool:
                        try:
                            return float(a) == float(b)
                        except Exception:
                            return False

                    if all(
                        (k in current_remaining) and _eq(v, current_remaining.get(k))
                        for k, v in overrides.items()
                    ):
                        overrides = None

            if overrides is None and data.get("leave_limits_override") is not None:
                pass
            elif data.get("leave_limits_override") is None:
                # explicit clear
                emp.leave_limits_override = None
                updates.append("leave_limits_override")
            else:
                existing = emp.leave_limits_override
                if not isinstance(existing, dict):
                    existing = {}
                merged = dict(existing)
                merged.update(overrides or {})

                # If admin sets Vacation explicitly, suppress prior-year carry-forward
                # for the remainder of the current leave year.
                if overrides and "VACATION" in overrides:
                    merged["_vacation_carry_reset_on"] = (
                        timezone.localdate().isoformat()
                    )
                emp.leave_limits_override = merged
                updates.append("leave_limits_override")

        if "manual_used_by_type" in data:
            used_map = data.get("manual_used_by_type")
            today = timezone.localdate()
            year_start, _year_end_excl = get_leave_year_range_for_employee(
                emp, on_date=today
            )

            existing = emp.leave_limits_override
            if not isinstance(existing, dict):
                existing = {}
            merged = dict(existing)

            by_year = merged.get("_manual_used_by_year")
            if not isinstance(by_year, dict):
                by_year = {}
            by_year = dict(by_year)

            key = year_start.isoformat()
            if used_map is None:
                by_year.pop(key, None)
            else:
                by_year[key] = dict(used_map)

            if by_year:
                merged["_manual_used_by_year"] = by_year
            else:
                merged.pop("_manual_used_by_year", None)

            emp.leave_limits_override = merged
            if "leave_limits_override" not in updates:
                updates.append("leave_limits_override")

        # If joining_date changed and probation wasn't explicitly provided, keep existing behavior
        if (
            joining_date is not None
            and "probation_end_date" not in data
            and "probation_period_days" not in data
        ):
            emp.probation_end_date = compute_probation_end_date(emp.joining_date)
            if "probation_end_date" not in updates:
                updates.append("probation_end_date")

        emp.name = (user.get_full_name() or "").strip() or user.username
        if "name" not in updates:
            updates.append("name")

        if updates:
            emp.save(update_fields=list(dict.fromkeys(updates)))

        return emp


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class ProjectCreateSerializer(serializers.Serializer):
    name = serializers.CharField()

    def validate(self, data: dict[str, Any]):
        name = (data.get("name") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Project name is required."})
        if Project.objects.filter(name__iexact=name).exists():
            raise serializers.ValidationError({"name": "Project already exists."})
        data["name"] = name
        return data

    def create(self, validated_data):
        return Project.objects.create(name=validated_data["name"], is_active=True)


class AllUsersDetailSerializer(serializers.ModelSerializer):
    profile = serializers.SerializerMethodField()
    employee = serializers.SerializerMethodField()

    name = serializers.SerializerMethodField()
    project = serializers.SerializerMethodField()
    joining_date = serializers.SerializerMethodField()
    last_request_sent = serializers.SerializerMethodField()
    is_on_probation = serializers.SerializerMethodField()

    total_leave_this_year = serializers.SerializerMethodField()
    leaves = serializers.SerializerMethodField()
    remaining_leaves = serializers.SerializerMethodField()
    recent_leaves = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "date_joined",
            "profile",
            "employee",
            "is_on_probation",
            "name",
            "project",
            "joining_date",
            "last_request_sent",
            "total_leave_this_year",
            "leaves",
            "remaining_leaves",
            "recent_leaves",
        ]

    # profile/user info

    def get_profile(self, obj):
        profile = getattr(obj, "profile", None)
        if not profile:
            return None
        return {"role": getattr(profile, "role", None)}

    def get_employee(self, obj):
        emp = getattr(obj, "employee", None)
        return emp.id if emp else None

    def get_is_on_probation(self, obj):
        emp = getattr(obj, "employee", None)
        fn = getattr(emp, "is_on_probation", None) if emp else None
        return bool(fn()) if callable(fn) else None

    def get_name(self, obj):
        emp = getattr(obj, "employee", None)
        if emp and getattr(emp, "name", None):
            return emp.name
        full = (obj.get_full_name() or "").strip()
        return full or obj.username

    def get_project(self, obj):
        emp = getattr(obj, "employee", None)
        return getattr(emp, "current_project", None) if emp else None

    def get_joining_date(self, obj):
        emp = getattr(obj, "employee", None)
        return getattr(emp, "joining_date", None) if emp else None

    # Leave meta
    def get_last_request_sent(self, obj):
        emp = getattr(obj, "employee", None)
        leaves = get_employee_leaves(emp) if emp else []
        return leaves[0].applied_at if leaves else None

    def get_recent_leaves(self, obj):
        emp = getattr(obj, "employee", None)
        leaves = (get_employee_leaves(emp) if emp else [])[:5]
        return LeaveMiniSerializer(leaves, many=True).data

    # Totals / used / remaining

    def get_total_leave_this_year(self, obj):
        emp = getattr(obj, "employee", None)
        leaves = get_employee_leaves(emp) if emp else []
        return total_leave_this_year(emp, leaves=leaves) if emp else 0

    def get_leaves(self, obj):
        emp = getattr(obj, "employee", None)
        leaves = get_employee_leaves(emp) if emp else []
        return used_leaves_by_type(emp, leaves=leaves) if emp else {}

    def get_remaining_leaves(self, obj):
        emp = getattr(obj, "employee", None)
        leaves = get_employee_leaves(emp) if emp else []
        return remaining_leaves(emp, leaves=leaves) if emp else {}


# Create / Update users


class AdminUserCreateSerializer(serializers.Serializer):
    username = serializers.CharField()
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)

    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    joining_date = serializers.DateField(required=False)

    reset_leave_balance = serializers.BooleanField(required=False)

    current_project = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )

    def validate(self, data: dict[str, Any]):
        username = (data.get("username") or "").strip()
        if not username:
            raise serializers.ValidationError({"username": "Username is required."})
        if User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError({"username": "Username already exists."})
        data["username"] = username

        email = (data.get("email") or "").strip().lower()
        if email:
            if User.objects.filter(email__iexact=email).exists():
                raise serializers.ValidationError({"email": "Email already exists."})
            data["email"] = email
        else:
            data["email"] = ""

        pwd = (data.get("password") or "").strip()
        cpwd = (data.get("confirm_password") or "").strip()
        if pwd != cpwd:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )
        if len(pwd) < 6:
            raise serializers.ValidationError(
                {"password": "Password must be at least 6 characters."}
            )

        joining_date = data.get("joining_date")
        if joining_date and joining_date > timezone.localdate():
            raise serializers.ValidationError(
                {"joining_date": "Joining date cannot be in the future."}
            )

        return data

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("confirm_password", None)
        password = validated_data.pop("password")
        joining_date = validated_data.pop("joining_date", None)
        reset_leave_balance = validated_data.pop("reset_leave_balance", None)
        current_project = validated_data.pop("current_project", None)

        user = User(**validated_data)
        user.set_password(password)
        user.is_staff = False
        user.is_superuser = False
        user.save()

        joining_date = joining_date or (
            timezone.localtime(user.date_joined).date()
            if user.date_joined
            else timezone.localdate()
        )

        employee, created_emp = Employee.objects.get_or_create(
            user=user,
            defaults={
                "name": (user.get_full_name() or "").strip() or user.username,
                "joining_date": joining_date,
                "probation_end_date": compute_probation_end_date(joining_date),
                "reset_leave_balance": bool(reset_leave_balance)
                if reset_leave_balance is not None
                else False,
                "current_project": (current_project or ""),
            },
        )

        if not created_emp:
            updates = []

            if employee.joining_date != joining_date:
                employee.joining_date = joining_date
                employee.probation_end_date = compute_probation_end_date(joining_date)
                updates.extend(["joining_date", "probation_end_date"])

            if reset_leave_balance is not None and employee.reset_leave_balance != bool(
                reset_leave_balance
            ):
                employee.reset_leave_balance = bool(reset_leave_balance)
                updates.append("reset_leave_balance")

            if current_project is not None:
                employee.current_project = current_project or ""
                updates.append("current_project")

            if updates:
                employee.save(update_fields=updates)

        if reset_leave_balance is None:
            today = timezone.localdate()
            leave_year_start, _ = get_leave_year_range_for_employee(
                employee, on_date=today
            )
            if joining_date < leave_year_start and not employee.reset_leave_balance:
                employee.reset_leave_balance = True
                employee.save(update_fields=["reset_leave_balance"])

        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"role": "EMPLOYEE", "employee": employee},
        )

        if profile.employee_id is None:
            profile.employee = employee
            profile.save(update_fields=["employee"])

        if profile.role != "EMPLOYEE":
            profile.role = "EMPLOYEE"
            profile.save(update_fields=["role"])

        return user


class AdminUserUpdateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()

    username = serializers.CharField(required=False)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False)

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    confirm_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    joining_date = serializers.DateField(required=False)
    is_on_probation = serializers.BooleanField(required=False)

    reset_leave_balance = serializers.BooleanField(required=False)

    current_project = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )

    # Optional per-employee leave limit overrides, e.g. {"VACATION": 18}
    # Use null to clear overrides.
    leave_limits_override = serializers.JSONField(required=False, allow_null=True)

    def validate(self, data: dict[str, Any]):
        user_id = data["user_id"]

        # Ensure user exists
        if not User.objects.filter(id=user_id).exists():
            raise serializers.ValidationError({"user_id": "User not found."})

        pwd = (data.get("password") or "").strip()
        cpwd = (data.get("confirm_password") or "").strip()
        if pwd or cpwd:
            if pwd != cpwd:
                raise serializers.ValidationError(
                    {"confirm_password": "Passwords do not match."}
                )
            if len(pwd) < 6:
                raise serializers.ValidationError(
                    {"password": "Password must be at least 6 characters."}
                )

        if "email" in data:
            email = (data.get("email") or "").strip().lower()
            if email:
                if (
                    User.objects.filter(email__iexact=email)
                    .exclude(id=user_id)
                    .exists()
                ):
                    raise serializers.ValidationError(
                        {"email": "Email already exists."}
                    )
                data["email"] = email
            else:
                data["email"] = ""

        if "username" in data:
            username = (data.get("username") or "").strip()
            if username:
                if (
                    User.objects.filter(username__iexact=username)
                    .exclude(id=user_id)
                    .exists()
                ):
                    raise serializers.ValidationError(
                        {"username": "Username already exists."}
                    )
                data["username"] = username
            else:
                raise serializers.ValidationError(
                    {"username": "Username cannot be empty."}
                )

        joining_date = data.get("joining_date")
        if joining_date and joining_date > timezone.localdate():
            raise serializers.ValidationError(
                {"joining_date": "Joining date cannot be in the future."}
            )

        if "leave_limits_override" in data:
            overrides = data.get("leave_limits_override")
            if overrides is None:
                # explicit clear
                pass
            elif not isinstance(overrides, dict):
                raise serializers.ValidationError(
                    {
                        "leave_limits_override": "Must be an object mapping leave types to numbers."
                    }
                )
            else:
                allowed = set(str(k).strip().upper() for k in LEAVE_LIMITS.keys())
                cleaned: dict[str, float] = {}
                for raw_key, raw_val in overrides.items():
                    if not isinstance(raw_key, str):
                        raise serializers.ValidationError(
                            {"leave_limits_override": "All keys must be strings."}
                        )
                    key = raw_key.strip()
                    if not key:
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": "Leave type keys cannot be empty."
                            }
                        )

                    key_norm = key.upper()
                    if key_norm not in allowed:
                        raise serializers.ValidationError(
                            {"leave_limits_override": f"Invalid leave type '{key}'."}
                        )

                    try:
                        num = float(raw_val)
                    except (TypeError, ValueError):
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": f"Value for '{key}' must be a number."
                            }
                        )

                    if num < 0:
                        raise serializers.ValidationError(
                            {
                                "leave_limits_override": f"Value for '{key}' cannot be negative."
                            }
                        )

                    cleaned[key_norm] = num

                data["leave_limits_override"] = cleaned

        return data

    @transaction.atomic
    def update_user(self):

        data = self.validated_data
        user = User.objects.select_for_update().get(id=data["user_id"])

        # Update user fields
        for f in ("username", "first_name", "last_name", "email"):
            if f in data:
                setattr(user, f, data[f])

        # Update password (optional)
        pwd = (data.get("password") or "").strip()
        if pwd:
            user.set_password(pwd)

        user.save()

        # Update employee fields
        emp = getattr(user, "employee", None)
        if emp:
            updates = []

            joining_date = data.get("joining_date", None)
            if joining_date is not None and emp.joining_date != joining_date:
                emp.joining_date = joining_date
                emp.probation_end_date = compute_probation_end_date(joining_date)
                updates.extend(["joining_date", "probation_end_date"])

            if "is_on_probation" in data:
                today = timezone.localdate()
                desired = bool(data.get("is_on_probation"))
                if desired:
                    # Ensure probation_end_date is at least today
                    new_end = max(today, emp.joining_date)
                else:
                    # Ensure not on probation by setting end date to before today.
                    new_end = today - timedelta(days=1)

                if emp.probation_end_date != new_end:
                    emp.probation_end_date = new_end
                    updates.append("probation_end_date")

            reset_leave_balance = data.get("reset_leave_balance", None)
            if reset_leave_balance is not None and emp.reset_leave_balance != bool(
                reset_leave_balance
            ):
                emp.reset_leave_balance = bool(reset_leave_balance)
                updates.append("reset_leave_balance")

            current_project = data.get("current_project", None)
            if current_project is not None:
                emp.current_project = current_project or ""
                updates.append("current_project")

            if "leave_limits_override" in data:
                overrides = data.get("leave_limits_override")
                if overrides is None:
                    emp.leave_limits_override = None
                else:
                    existing = emp.leave_limits_override
                    if not isinstance(existing, dict):
                        existing = {}
                    merged = dict(existing)
                    merged.update(overrides)

                    # If admin sets Vacation explicitly, suppress prior-year carry-forward
                    # for the remainder of the current leave year.
                    if "VACATION" in overrides:
                        merged["_vacation_carry_reset_on"] = (
                            timezone.localdate().isoformat()
                        )
                    emp.leave_limits_override = merged
                updates.append("leave_limits_override")

            if updates:
                emp.save(update_fields=updates)

        return user


class LeavePolicySettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeavePolicySettings
        fields = [
            "carryover_percentage",
            "probation_period_days",
            "vacation_days",
            "sick_days",
            "maternity_days",
            "paternity_days",
            "bereavement_days",
        ]

    def validate_carryover_percentage(self, value):
        if value is None:
            return value
        if value < 0 or value > 100:
            raise serializers.ValidationError(
                "Carryover percentage must be between 0 and 100."
            )
        return value

    def validate_probation_period_days(self, value):
        if value is None:
            return value
        if value < 0:
            raise serializers.ValidationError(
                "Probation period days must be 0 or greater."
            )
        return value

    def validate(self, data: dict[str, Any]):
        for field in (
            "vacation_days",
            "sick_days",
            "maternity_days",
            "paternity_days",
            "bereavement_days",
        ):
            if field in data and data[field] is not None and data[field] < 0:
                raise serializers.ValidationError(
                    {field: "Value must be 0 or greater."}
                )
        return data


class EmployeeRenewalScheduleSerializer(serializers.Serializer):
    employee_id = serializers.IntegerField(source="id", read_only=True)
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    name = serializers.CharField(read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    joining_date = serializers.DateField(read_only=True)
    probation_end_date = serializers.DateField(read_only=True)
    leave_renewal_date_override = serializers.DateField(read_only=True)
    next_renewal_date = serializers.DateField(read_only=True)


class LeaveRenewalOverrideSerializer(serializers.Serializer):
    employee_id = serializers.IntegerField()
    leave_renewal_date_override = serializers.DateField(required=False, allow_null=True)

    def validate(self, data: dict[str, Any]):
        employee_id = data["employee_id"]
        if not Employee.objects.filter(id=employee_id).exists():
            raise serializers.ValidationError({"employee_id": "Employee not found."})

        # Note: leave_renewal_date_override is used as a month/day anchor when
        # computing leave-year windows; the year portion is not semantically
        # important, so past dates are valid.
        return data
