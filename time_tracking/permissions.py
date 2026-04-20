from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist


def can_manage_time_projects(user) -> bool:
    if not user or not user.is_authenticated:
        return False

    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True

    try:
        profile = user.profile
    except ObjectDoesNotExist:
        profile = None

    role = getattr(profile, "role", "")
    return role in {"ADMIN", "MANAGER"}
