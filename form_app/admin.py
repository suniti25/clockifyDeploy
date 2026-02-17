from django.contrib import admin
from django.contrib import messages

from .models import LeaveRequest
from integrations.services import sync_approved_leave_to_google


@admin.action(description="Sync selected approved leaves to Google Calendar")
def sync_selected_leaves_to_google(modeladmin, request, queryset):
    success = 0
    failed = 0
    for leave in queryset:
        owner_user = getattr(getattr(leave, "employee", None), "user", None)
        ok = sync_approved_leave_to_google(leave, user=owner_user or request.user)
        if ok:
            success += 1
        else:
            failed += 1

    if success:
        messages.success(request, f"Synced {success} leave(s) to Google Calendar")
    if failed:
        messages.warning(request, f"Failed to sync {failed} leave(s) to Google Calendar")


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "employee", "leave_type", "status", "start_date", "end_date", "google_event_id")
    list_filter = ("leave_type", "status")
    search_fields = ("employee__user__username", "employee__user__email", "employee__name")
    actions = [sync_selected_leaves_to_google]