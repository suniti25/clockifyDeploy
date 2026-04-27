from django.contrib import admin

from .models import TimeEntry


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "project", "started_at", "ended_at", "created_at")
    list_filter = ("started_at",)
    search_fields = ("description", "user__username")
