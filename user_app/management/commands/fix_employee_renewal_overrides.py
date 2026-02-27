"""Compatibility command.

Prefer running `python manage.py repair_employee_renewal_anchors`.
This command remains to avoid breaking existing deploy scripts.
"""

from .repair_employee_renewal_anchors import Command  # noqa: F401
