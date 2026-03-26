import re
from datetime import date as date_type

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ValidationError

from .models import Holiday, LeavePolicySettings, LeaveRequest
from integrations.services import sync_approved_leave_to_google


def _parse_bulk_holiday_dates(raw: str) -> list[date_type]:
    """Parse comma/newline/whitespace-separated YYYY-M-D dates into a sorted unique list."""
    if not raw:
        return []

    tokens = [t.strip() for t in re.split(r"[\s,]+", raw.strip()) if t.strip()]
    parsed: list[date_type] = []
    invalid: list[str] = []

    for token in tokens:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token)
        if not match:
            invalid.append(token)
            continue

        year, month, day = (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
        try:
            parsed.append(date_type(year, month, day))
        except ValueError:
            invalid.append(token)

    if invalid:
        raise ValidationError(
            "Invalid date(s): %(invalid)s. Use YYYY-MM-DD (or YYYY-M-D) separated by commas/new lines.",
            params={"invalid": ", ".join(invalid)},
        )

    # Deduplicate while preserving meaning; admin will show ordering anyway.
    return sorted(set(parsed))


class HolidayAdminForm(forms.ModelForm):
    bulk_dates = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "cols": 40}),
        help_text=(
            "Optional: add multiple holidays at once. Paste dates separated by commas/new lines, e.g. "
            "2026-01-02, 2026-01-05, 2026-01-6. "
            "Name/description/is_active will be applied to all created dates."
        ),
        label="Bulk dates",
    )

    class Meta:
        model = Holiday
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Bulk insert only makes sense on the "Add" form.
        if getattr(self.instance, "pk", None):
            self.fields.pop("bulk_dates", None)
        else:
            # Allow leaving date blank when bulk_dates is provided.
            self.fields["date"].required = False

    def clean(self):
        cleaned = super().clean()

        # On edit pages, bulk_dates is removed; only validate single date.
        if getattr(self.instance, "pk", None):
            return cleaned

        raw_bulk = (cleaned.get("bulk_dates") or "").strip()
        single_date = cleaned.get("date")

        if not raw_bulk and not single_date:
            raise ValidationError("Provide either a single Date or Bulk dates.")

        if raw_bulk and single_date:
            raise ValidationError(
                "Provide either a single Date or Bulk dates, not both."
            )

        if raw_bulk:
            dates = _parse_bulk_holiday_dates(raw_bulk)
            if not dates:
                raise ValidationError("No valid dates found in Bulk dates.")

            existing = set(
                Holiday.objects.filter(date__in=dates).values_list("date", flat=True)
            )
            to_create = [d for d in dates if d not in existing]

            if not to_create:
                raise ValidationError(
                    "All provided dates already exist as holidays; nothing new to create."
                )

            cleaned["_bulk_dates_to_create"] = to_create
            cleaned["_bulk_dates_existing"] = sorted(existing)

        return cleaned


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
        messages.warning(
            request, f"Failed to sync {failed} leave(s) to Google Calendar"
        )


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "employee",
        "leave_type",
        "status",
        "start_date",
        "end_date",
        "google_event_id",
    )
    list_filter = ("leave_type", "status")
    search_fields = (
        "employee__user__username",
        "employee__user__email",
        "employee__name",
    )
    actions = [sync_selected_leaves_to_google]


@admin.register(LeavePolicySettings)
class LeavePolicySettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "carryover_percentage",
        "probation_period_days",
        "vacation_days",
        "sick_days",
        "maternity_days",
        "paternity_days",
        "bereavement_days",
        "updated_at",
    )

    def has_add_permission(self, request):
        return not LeavePolicySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    form = HolidayAdminForm
    list_display = ("date", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    ordering = ("-date",)

    def get_fieldsets(self, request, obj=None):
        # `bulk_dates` is add-only. Including it on change pages causes admin
        # to look up a field removed by the form __init__, triggering KeyError.
        if obj is None:
            return (
                (
                    None,
                    {
                        "fields": (
                            "date",
                            "bulk_dates",
                            "name",
                            "description",
                            "is_active",
                        )
                    },
                ),
            )

        return (
            (
                None,
                {
                    "fields": (
                        "date",
                        "name",
                        "description",
                        "is_active",
                    )
                },
            ),
        )

    def save_model(self, request, obj, form, change):
        bulk_dates: list[date_type] = form.cleaned_data.get("_bulk_dates_to_create", [])

        if not change and bulk_dates:
            # Save the first new date as the "main" object so Django admin logging works.
            first, rest = bulk_dates[0], bulk_dates[1:]
            obj.date = first
            super().save_model(request, obj, form, change)

            if rest:
                Holiday.objects.bulk_create(
                    [
                        Holiday(
                            date=d,
                            name=obj.name,
                            description=obj.description,
                            is_active=obj.is_active,
                        )
                        for d in rest
                    ],
                )

            existing_dates: list[date_type] = form.cleaned_data.get(
                "_bulk_dates_existing", []
            )
            created_count = 1 + len(rest)
            messages.success(request, f"Created {created_count} holiday(s).")
            if existing_dates:
                messages.warning(
                    request,
                    "Skipped existing holiday date(s): "
                    + ", ".join(d.isoformat() for d in existing_dates),
                )
            return

        super().save_model(request, obj, form, change)
