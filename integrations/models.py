from django.conf import settings
from django.db import models


class GoogleCalendarCredential(models.Model):
  
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    google_email = models.EmailField(blank=True, default="")
    calendar_id = models.CharField(max_length=255, default="primary")

    refresh_token_encrypted = models.TextField() # encrypted
    access_token = models.TextField(blank=True, default="")
    token_expiry = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"GoogleCalendarCredential(user_id={self.user_id}, email={self.google_email})"
