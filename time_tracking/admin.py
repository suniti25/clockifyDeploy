from django.contrib import admin

from .models import TimeEntry, TimeProject


@admin.register(TimeProject)
class TimeProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "is_archived", "created_at")
    list_filter = ("is_archived",)
    search_fields = ("name", "user__username")


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "project", "started_at", "ended_at", "created_at")
    list_filter = ("started_at",)
    search_fields = ("description", "user__username")
