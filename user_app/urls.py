from django.urls import path
from . import views

urlpatterns = [
    # Profile
    path("me/", views.me, name="user-me"),

    # Dashboard
    path("dashboard/", views.hello_dashboard, name="user-dashboard"),
    path("leave-balances/", views.get_leave_balances, name="user-leave-balances"),

    # History & Recent
    path("history/", views.get_history, name="user-history"),
    path("recent-activities/", views.get_recent_activities, name="user-recent-activities"),

    #Upcoming Leaves
    path("upcoming-leaves/", views.get_upcoming_leaves, name="user-upcoming-leaves"),
    # Calendar
    path("calendar/", views.get_calendar_days, name="user-calendar"),
]