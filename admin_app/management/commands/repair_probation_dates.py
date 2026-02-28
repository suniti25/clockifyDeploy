from __future__ import annotations

from django.core.management.base import BaseCommand

from form_app.policies import compute_probation_end_date
from user_app.models import Employee


class Command(BaseCommand):
    help = (
        "Repair Employee.probation_end_date from joining_date using policy probation days. "
        "Dry-run by default; use --apply to persist changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--employee-id",
            type=int,
            action="append",
            dest="employee_ids",
            help="Target one or more employee IDs (repeat flag).",
        )
        parser.add_argument(
            "--all-mismatched",
            action="store_true",
            help="Include all employees where probation_end_date differs from policy-derived date.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist updates. Without this, command prints what would change.",
        )

    def handle(self, *args, **options):
        employee_ids = options.get("employee_ids") or []
        all_mismatched = bool(options.get("all_mismatched"))
        apply_changes = bool(options.get("apply"))

        if not employee_ids and not all_mismatched:
            self.stdout.write(
                self.style.WARNING(
                    "No target selected. Use --employee-id <id> (repeatable) "
                    "or --all-mismatched."
                )
            )
            return

        qs = Employee.objects.select_related("user").order_by("id")
        if employee_ids:
            qs = qs.filter(id__in=employee_ids)

        examined = 0
        changed = 0

        for emp in qs:
            examined += 1
            joining_date = getattr(emp, "joining_date", None)
            if not joining_date:
                continue

            expected = compute_probation_end_date(joining_date)
            current = getattr(emp, "probation_end_date", None)

            if current == expected:
                continue

            # If a specific employee-id is provided, always include mismatch.
            # If --all-mismatched is used, include every mismatch.
            if not all_mismatched and emp.id not in employee_ids:
                continue

            changed += 1
            user = getattr(emp, "user", None)
            username = getattr(user, "username", "") if user else ""

            self.stdout.write(
                f"employee_id={emp.id} username={username} "
                f"joining_date={joining_date} "
                f"probation_end_date: {current} -> {expected}"
            )

            if apply_changes:
                emp.probation_end_date = expected
                emp.save(update_fields=["probation_end_date"])

        mode = "APPLIED" if apply_changes else "DRY-RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}: examined={examined}, mismatched_selected={changed}"
            )
        )
