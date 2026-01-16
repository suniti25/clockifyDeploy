from django.db import models

discord_message_id = models.CharField(
    max_length=50,
    blank=True,
    null=True
)
