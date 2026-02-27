from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from form_app.policies import compute_probation_end_date
from user_app.models import Employee


class Command(BaseCommand):
    help = (
        "Fix employee probation_end_date (optional) and clear mismatched "
        "leave_renewal_date_override values so renewals align consistently.\n\n"
        "Why this exists: renewal calculations use leave_renewal_date_override (month/day) "
        "when set. If overrides were set unintentionally, some employees can get an incorrect "
        "next renewal date (e.g., anchored to today's month/day)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes (default is dry-run).",
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
                "computed anchor (probation_end_date + 1 day). Enabled by default."
            ),
        )

    def handle(self, *args, **options):
        apply_changes: bool = bool(options.get("apply"))
        fix_probation_end: bool = bool(options.get("fix_probation_end"))
        clear_mismatched: bool = bool(options.get("clear_mismatched_overrides"))

        employees = Employee.objects.select_related("user").all().order_by("id")

        probation_updates = 0
        override_clears = 0

        def _anchor_from_employee(emp: Employee):
            probation_end = getattr(emp, "probation_end_date", None)
            joining_date = getattr(emp, "joining_date", None)
            if probation_end:
                return probation_end + timedelta(days=1)
            if joining_date:
                return compute_probation_end_date(joining_date) + timedelta(days=1)
            return None

        changes_preview: list[str] = []

        with transaction.atomic():
            for emp in employees:
                updates: list[str] = []

                joining_date = getattr(emp, "joining_date", None)
                if fix_probation_end and joining_date:
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
                # ensure dry-run doesn't persist anything
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {employees.count()} employees. "
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
