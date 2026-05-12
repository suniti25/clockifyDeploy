from __future__ import annotations

from rest_framework import serializers
from user_app.models import Project

from . import services
from .models import TimeEntry


class TimeProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "is_active", "color", "created_at"]
        read_only_fields = ["id", "created_at"]


class TimeEntrySerializer(serializers.ModelSerializer):
    duration_seconds = serializers.SerializerMethodField()
    is_running = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    project_color = serializers.SerializerMethodField()

    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "user",
            "project",
            "description",
            "entry_type",
            "started_at",
            "ended_at",
            "duration_seconds",
            "is_running",
            "user_name",
            "project_name",
            "project_color",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "user",
            "created_at",
            "duration_seconds",
            "is_running",
            "user_name",
            "project_name",
            "project_color",
        ]

    def get_duration_seconds(self, obj: TimeEntry):
        if obj.ended_at is not None:
            return obj.duration_seconds
        if not obj.started_at:
            return None
        from django.utils import timezone

        delta = timezone.now() - obj.started_at
        return max(int(delta.total_seconds()), 0)

    def get_is_running(self, obj: TimeEntry) -> bool:
        return obj.is_running

    def get_user_name(self, obj: TimeEntry) -> str:
        full_name = obj.user.get_full_name()
        return full_name or obj.user.username

    def get_project_name(self, obj: TimeEntry) -> str | None:
        if not obj.project:
            return None
        return obj.project.name

    def get_project_color(self, obj: TimeEntry) -> str | None:
        if not obj.project:
            return None
        return obj.project.color

    def validate(self, attrs):
        started = attrs.get("started_at") or getattr(
            self.instance, "started_at", None
        )
        ended = attrs.get("ended_at")
        if ended is None and self.instance is not None:
            ended = self.instance.ended_at
        if started and ended and ended < started:
            raise serializers.ValidationError(
                {"ended_at": "ended_at cannot be before started_at."}
            )
        project = attrs.get("project")
        if "project" not in attrs and self.instance is not None:
            project = self.instance.project
        if ended is not None and project is None:
            raise serializers.ValidationError(
                {"project": "A stopped time entry must have a project."}
            )
        return attrs

    def validate_project(self, value: Project | None):
        if value is None:
            return value
        if not value.is_active:
            if self.instance is not None and self.instance.project_id == value.pk:
                return value
            raise serializers.ValidationError("Project is inactive.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        if validated_data.get("ended_at") is None:
            if services.get_running_entry(request.user) is not None:
                raise serializers.ValidationError(
                    {
                        "ended_at": "Stop the running timer before creating another open-ended entry."
                    }
                )
        return TimeEntry.objects.create(user=request.user, **validated_data)


class TimeEntryStartSerializer(serializers.Serializer):
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.none(), required=False, allow_null=True
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")
    entry_type = serializers.ChoiceField(
        choices=TimeEntry.ENTRY_TYPE_CHOICES,
        required=False,
        allow_blank=True,
        default="",
    )
    started_at = serializers.DateTimeField(required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(is_active=True)


class TimeEntryStopSerializer(serializers.Serializer):
    entry_id = serializers.IntegerField(required=False, allow_null=True)
