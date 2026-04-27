from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from user_app.models import Project


class TimeEntry(models.Model):
    """A single time segment; `ended_at` null means timer is running."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="time_entries",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="entries",
    )
    description = models.CharField(max_length=500, blank=True, default="")
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["user", "ended_at"]),
            models.Index(fields=["user", "started_at"]),
        ]

    def clean(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValidationError("ended_at cannot be before started_at.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_running(self) -> bool:
        return self.ended_at is None

    def duration_seconds(self) -> int | None:
        if not self.started_at or not self.ended_at:
            return None
        delta = self.ended_at - self.started_at
        return max(int(delta.total_seconds()), 0)

    def __str__(self) -> str:
        return f"{self.user_id}:{self.started_at.isoformat()}"
