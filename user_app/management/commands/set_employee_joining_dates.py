from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from user_app.models import Employee


def _parse_pair(pair: str) -> tuple[str, date]:
    if not pair or "=" not in str(pair):
        raise CommandError(
            "Invalid --pair format. Use --pair username=YYYY-MM-DD (repeatable)."
        )
    username, raw_date = pair.split("=", 1)
    username = (username or "").strip()
    raw_date = (raw_date or "").strip()
    if not username:
        raise CommandError("Invalid --pair: missing username")
    try:
        d = date.fromisoformat(raw_date[:10])
    except Exception as exc:
        raise CommandError(
            f"Invalid --pair date for {username!r}: {raw_date!r}. Expected YYYY-MM-DD"
        ) from exc
    return username, d


class Command(BaseCommand):
    help = (
        "Set Employee.joining_date for specific users by username. "
        "This is useful because probation end and leave renewal calculations depend on joining_date."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes (default is dry-run).",
        )
        parser.add_argument(
            "--pair",
            action="append",
            default=[],
            help=(
                "Username/date pair in the form username=YYYY-MM-DD. "
                "Repeat for multiple users."
            ),
        )
        parser.add_argument(
            "--clear-renewal-override",
            action="store_true",
            help=(
                "Also clear Employee.leave_renewal_date_override for touched employees "
                "so renewals immediately follow the default policy anchor (probation_end_date month/day)."
            ),
        )

    def handle(self, *args, **options):
        apply_changes: bool = bool(options.get("apply"))
        pairs_raw: list[str] = list(options.get("pair") or [])
        clear_override: bool = bool(options.get("clear_renewal_override"))

        if not pairs_raw:
            raise CommandError("Provide at least one --pair username=YYYY-MM-DD")

        wanted: dict[str, date] = {}
        for p in pairs_raw:
            username, d = _parse_pair(p)
            wanted[username] = d

        employees = (
            Employee.objects.select_related("user")
            .filter(user__username__in=list(wanted.keys()))
            .order_by("id")
        )

        found_usernames = set(
            e.user.username for e in employees if getattr(e, "user", None)
        )
        missing = [u for u in wanted.keys() if u not in found_usernames]
        if missing:
            raise CommandError(f"No Employee found for usernames: {', '.join(missing)}")

        changes_preview: list[str] = []
        joining_updates = 0
        override_clears = 0

        with transaction.atomic():
            for emp in employees:
                username = emp.user.username
                target_date = wanted[username]

                updates: list[str] = []

                if getattr(emp, "joining_date", None) != target_date:
                    emp.joining_date = target_date
                    updates.append("joining_date")
                    joining_updates += 1

                if clear_override and getattr(emp, "leave_renewal_date_override", None):
                    emp.leave_renewal_date_override = None
                    updates.append("leave_renewal_date_override")
                    override_clears += 1

                if updates:
                    changes_preview.append(
                        f"Employee {emp.id} ({username}): {', '.join(updates)}"
                    )
                    if apply_changes:
                        emp.save(update_fields=updates)

            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Matched {employees.count()} employees. "
                f"joining_date updates: {joining_updates}, "
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
