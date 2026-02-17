from django.core.exceptions import ObjectDoesNotExist

from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        # Support Django's built-in admin users even if no Profile row exists.
        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True

        try:
            profile = user.profile
        except ObjectDoesNotExist:
            profile = None

        return bool(profile and profile.role == "ADMIN")
