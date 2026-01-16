from django.urls import path
from . import views

urlpatterns = [
    path('me/', views.me, name='me'),
    path('dashboard/', views.hello_dashboard, name='hello_dashboard'),
    path('leave-balances/', views.get_leave_balances, name='leave_balances'),
    path('notifications/', views.get_notifications, name='notifications'),#not working
    path('recent-activities/', views.get_recent_activities, name='recent_activities'),
    path('calendar-days/', views.get_calendar_days, name='calendar_days'),
]