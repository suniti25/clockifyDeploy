from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from .models import TimeEntry


def get_running_entry(user: User) -> TimeEntry | None:
    return (
        TimeEntry.objects.filter(user=user, ended_at__isnull=True)
        .select_related("project")
        .order_by("-started_at")
        .first()
    )


@transaction.atomic
def start_timer(
    *,
    user: User,
    project_id: int | None,
    description: str,
    started_at,
) -> TimeEntry:
    if get_running_entry(user) is not None:
        raise ValueError("A timer is already running. Stop it before starting a new one.")
    entry = TimeEntry.objects.create(
        user=user,
        project_id=project_id,
        description=description or "",
        started_at=started_at or timezone.now(),
        ended_at=None,
    )
    return entry


@transaction.atomic
def stop_running_timer(*, user: User, entry_id: int | None = None) -> TimeEntry:
    qs = TimeEntry.objects.select_for_update().filter(user=user, ended_at__isnull=True)
    if entry_id is not None:
        qs = qs.filter(pk=entry_id)
    entry = qs.order_by("-started_at").first()
    if entry is None:
        raise ValueError("No running timer found.")
    entry.ended_at = timezone.now()
    entry.save(update_fields=["ended_at"])
    return entry
