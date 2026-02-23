from django.contrib import admin

from integrations.models import GoogleCalendarCredential


@admin.register(GoogleCalendarCredential)
class GoogleCalendarCredentialAdmin(admin.ModelAdmin):
    list_display = ("user", "google_email", "calendar_id", "updated_at")
    search_fields = ("user__username", "user__email", "google_email", "calendar_id")
    readonly_fields = (
        "refresh_token_encrypted",
        "access_token",
        "token_expiry",
        "created_at",
        "updated_at",
    )
