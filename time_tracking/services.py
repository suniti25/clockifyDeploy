from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from user_app.models import Project

from .models import TimeEntry

UNSET = object()


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


@transaction.atomic
def update_entry(
    *,
    user: User,
    entry: TimeEntry,
    project: Project | None | object = UNSET,
    description: str | None | object = UNSET,
    started_at=UNSET,
    ended_at=UNSET,
) -> TimeEntry:
    if entry.user_id != user.id:
        raise ValueError("Entry does not belong to user.")

    if project is not UNSET:
        if isinstance(project, Project):
            if not project.is_active:
                raise ValueError("Project is inactive.")
            entry.project = project
        else:
            entry.project = None

    if description is not UNSET:
        entry.description = description

    if started_at is not UNSET:
        entry.started_at = started_at

    if ended_at is not UNSET:
        entry.ended_at = ended_at

    if entry.ended_at is None:
        has_other_running = (
            TimeEntry.objects.filter(user=user, ended_at__isnull=True)
            .exclude(pk=entry.pk)
            .exists()
        )
        if has_other_running:
            raise ValueError(
                "Another timer is already running. Stop it before reopening this entry."
            )

    if entry.started_at and entry.ended_at and entry.ended_at < entry.started_at:
        raise ValueError("ended_at cannot be before started_at.")

    entry.save()
    return entry


@transaction.atomic
def continue_entry(*, user: User, source_entry: TimeEntry) -> TimeEntry:
    if source_entry.user_id != user.id:
        raise ValueError("Entry does not belong to user.")
    if source_entry.ended_at is None:
        raise ValueError("Cannot continue a running entry.")
    if get_running_entry(user) is not None:
        raise ValueError("A timer is already running. Stop it before continuing.")

    return TimeEntry.objects.create(
        user=user,
        project=source_entry.project,
        description=source_entry.description or "",
        started_at=timezone.now(),
        ended_at=None,
    )


@transaction.atomic
def duplicate_entry(*, user: User, source_entry: TimeEntry) -> TimeEntry:
    if source_entry.user_id != user.id:
        raise ValueError("Entry does not belong to user.")
    if source_entry.ended_at is None:
        raise ValueError("Cannot duplicate a running entry.")

    return TimeEntry.objects.create(
        user=user,
        project=source_entry.project,
        description=source_entry.description or "",
        started_at=source_entry.started_at,
        ended_at=source_entry.ended_at,
    )
