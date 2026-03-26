from __future__ import annotations

from django.db import models


class DiscordDailyMessage(models.Model):
    """Stores a single Discord message per day that we can edit in-place."""

    KEY_EMPLOYEE_ON_LEAVE_TODAY = "EMPLOYEE_ON_LEAVE_TODAY"
    KEY_CHOICES = [(KEY_EMPLOYEE_ON_LEAVE_TODAY, "Employee: On Leave Today")]

    key = models.CharField(max_length=64, choices=KEY_CHOICES)
    target_date = models.DateField()
    channel_id = models.BigIntegerField()
    message_id = models.CharField(max_length=64)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key", "target_date"], name="uniq_discord_daily_message"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.key} {self.target_date} ({self.channel_id}/{self.message_id})"
