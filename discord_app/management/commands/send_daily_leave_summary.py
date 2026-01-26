from django.core.management.base import BaseCommand
from form_app.models import LeaveRequest
from discord_app.services import send_daily_summary
from datetime import date, timezone
from collections import defaultdict
from datetime import timedelta


class Command(BaseCommand):
    help = 'Send daily summary of approved leaves to Discord at 10:30 AM'

    def handle(self, *args, **options):
        today = timezone.localdate()
        approved_requests = LeaveRequest.objects.filter(
            status="APPROVED",
            start_date__lte=today,
            end_date__gte=today,
        ).select_related("employee")
        
        leaves_by_date = defaultdict(list)
        for leave in approved_requests:
            current = leave.start_date
            while current <= leave.end_date:
                leaves_by_date[current].append(leave)
                current += timedelta(days=1)
        
        if leaves_by_date:
            success = send_daily_summary(dict(leaves_by_date))
            if success:
                self.stdout.write(self.style.SUCCESS('Daily summary sent successfully'))
            else:
                self.stdout.write(self.style.ERROR('Failed to send daily summary'))
        else:
            self.stdout.write(self.style.WARNING('No approved leaves for today'))
