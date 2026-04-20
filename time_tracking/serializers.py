from __future__ import annotations

from rest_framework import serializers

from . import services
from .models import TimeEntry, TimeProject


class TimeProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeProject
        fields = ["id", "name", "color", "is_archived", "created_at"]
        read_only_fields = ["id", "created_at"]


class TimeEntrySerializer(serializers.ModelSerializer):
    duration_seconds = serializers.SerializerMethodField()
    is_running = serializers.SerializerMethodField()

    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "project",
            "description",
            "started_at",
            "ended_at",
            "duration_seconds",
            "is_running",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "duration_seconds", "is_running"]

    def get_duration_seconds(self, obj: TimeEntry):
        return obj.duration_seconds()

    def get_is_running(self, obj: TimeEntry) -> bool:
        return obj.is_running

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
        return attrs

    def validate_project(self, value: TimeProject | None):
        request = self.context.get("request")
        if value is None or request is None:
            return value
        if value.user_id != request.user.id:
            raise serializers.ValidationError("Invalid project for this user.")
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
        queryset=TimeProject.objects.none(), required=False, allow_null=True
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")
    started_at = serializers.DateTimeField(required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None:
            self.fields["project"].queryset = TimeProject.objects.filter(
                user=request.user, is_archived=False
            )


class TimeEntryStopSerializer(serializers.Serializer):
    entry_id = serializers.IntegerField(required=False, allow_null=True)
