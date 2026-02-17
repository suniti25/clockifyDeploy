from django.urls import path
from .views import discord_interactions, cron_daily_on_leave, cron_daily_approved

urlpatterns = [
    path("interactions/", discord_interactions, name="discord_interactions"),
    path("interactions", discord_interactions),

    # Employee channel: on leave today
    path("cron/daily-on-leave/", cron_daily_on_leave, name="cron_daily_on_leave"),
    path("cron/daily-on-leave", cron_daily_on_leave),

    # Admin channel: approved today
    path("cron/daily-approved/", cron_daily_approved, name="cron_daily_approved"),
    path("cron/daily-approved", cron_daily_approved)
]
