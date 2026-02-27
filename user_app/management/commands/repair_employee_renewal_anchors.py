from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from form_app.policies import compute_probation_end_date
from user_app.models import Employee


class Command(BaseCommand):
    help = (
        "Repair employee renewal anchors and joining/probation dates.\n\n"
        "Renewal calculations use Employee.leave_renewal_date_override as a month/day anchor when set. "
        "If overrides were set unintentionally, some employees can show an incorrect "
        "next renewal date. This command clears mismatched overrides so renewals align with the default policy "
        "(probation_end_date month/day)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes (default is dry-run).",
        )
        parser.add_argument(
            "--sync-joining-date-from-user-date-joined",
            action="store_true",
            help=(
                "Set Employee.joining_date = user.date_joined (date part) when different. "
                "Useful when employees were created with a default joining_date (today)."
            ),
        )
        parser.add_argument(
            "--fix-probation-end",
            action="store_true",
            help=(
                "Recompute probation_end_date from joining_date using current policy "
                "and update if different."
            ),
        )
        parser.add_argument(
            "--clear-mismatched-overrides",
            action="store_true",
            default=True,
            help=(
                "Clear leave_renewal_date_override when its month/day doesn't match the "
                "computed anchor (probation_end_date month/day). Enabled by default."
            ),
        )
        parser.add_argument(
            "--no-clear-mismatched-overrides",
            action="store_false",
            dest="clear_mismatched_overrides",
            help=(
                "Do not clear leave_renewal_date_override values (useful when you intentionally maintain per-employee exceptions)."
            ),
        )

    def handle(self, *args, **options):
        apply_changes: bool = bool(options.get("apply"))
        sync_joining_date: bool = bool(
            options.get("sync_joining_date_from_user_date_joined")
        )
        fix_probation_end: bool = bool(options.get("fix_probation_end"))
        clear_mismatched: bool = bool(options.get("clear_mismatched_overrides"))

        employees = Employee.objects.select_related("user").all().order_by("id")

        joining_updates = 0
        probation_updates = 0
        override_clears = 0

        def _expected_joining_from_user(emp: Employee):
            user = getattr(emp, "user", None)
            dj = getattr(user, "date_joined", None) if user else None
            if not dj:
                return None
            try:
                return timezone.localtime(dj).date()
            except Exception:
                try:
                    return dj.date()
                except Exception:
                    return None

        def _anchor_from_employee(emp: Employee):
            joining_date = getattr(emp, "joining_date", None)
            probation_end = getattr(emp, "probation_end_date", None)

            # Default renewal anchor is the employee's probation end month/day.
            # If probation_end_date is missing, compute it from joining_date.
            # If probation was explicitly ended early by setting probation_end_date < joining_date
            # (e.g. joining_date - 1), treat it as "no probation" and anchor to joining_date.
            if joining_date and probation_end and probation_end < joining_date:
                return joining_date

            if probation_end:
                return probation_end

            if joining_date:
                try:
                    return compute_probation_end_date(joining_date)
                except Exception:
                    return joining_date

            return None

        changes_preview: list[str] = []

        with transaction.atomic():
            for emp in employees:
                updates: list[str] = []

                if sync_joining_date:
                    expected_joining = _expected_joining_from_user(emp)
                    if (
                        expected_joining
                        and getattr(emp, "joining_date", None) != expected_joining
                    ):
                        updates.append("joining_date")
                        emp.joining_date = expected_joining
                        joining_updates += 1

                joining_date = getattr(emp, "joining_date", None)
                overrides = getattr(emp, "leave_limits_override", None)
                has_probation_override = bool(
                    isinstance(overrides, dict)
                    and overrides.get("_probation_end_date_override")
                )

                if fix_probation_end and joining_date and not has_probation_override:
                    expected_prob_end = compute_probation_end_date(joining_date)
                    if getattr(emp, "probation_end_date", None) != expected_prob_end:
                        updates.append("probation_end_date")
                        emp.probation_end_date = expected_prob_end
                        probation_updates += 1

                if clear_mismatched:
                    override = getattr(emp, "leave_renewal_date_override", None)
                    if override:
                        anchor = _anchor_from_employee(emp)
                        if anchor and (override.month, override.day) != (
                            anchor.month,
                            anchor.day,
                        ):
                            updates.append("leave_renewal_date_override")
                            emp.leave_renewal_date_override = None
                            override_clears += 1

                if updates:
                    who = (
                        emp.user.username if getattr(emp, "user", None) else str(emp.id)
                    )
                    changes_preview.append(
                        f"Employee {emp.id} ({who}): {', '.join(updates)}"
                    )
                    if apply_changes:
                        emp.save(update_fields=updates)

            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {employees.count()} employees. "
                f"joining_date updates: {joining_updates}, "
                f"probation_end_date updates: {probation_updates}, "
                f"override clears: {override_clears}. "
                f"Mode: {'APPLY' if apply_changes else 'DRY-RUN'}"
            )
        )

        if changes_preview:
            self.stdout.write("\nPlanned changes:")
            for line in changes_preview[:200]:
                self.stdout.write(f"- {line}")
            if len(changes_preview) > 200:
                self.stdout.write(f"... and {len(changes_preview) - 200} more")
        else:
            self.stdout.write("No changes needed.")
