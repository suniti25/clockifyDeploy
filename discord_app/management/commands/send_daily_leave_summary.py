from django.core.management.base import BaseCommand
from django.utils import timezone

from discord_app.services import _active_today_qs, send_employee_on_leave_today


class Command(BaseCommand):
    help = "Send daily summary of approved leaves to Discord at 10:30 AM"

    def handle(self, *args, **options):
        today = timezone.localdate()

        active_count = _active_today_qs(today).count()

        if active_count:
            success = send_employee_on_leave_today()
            if success:
                self.stdout.write(self.style.SUCCESS("Daily summary sent successfully"))
            else:
                self.stdout.write(self.style.ERROR("Failed to send daily summary"))
        else:
            self.stdout.write(self.style.WARNING("No approved leaves for today"))
