# from django.urls import path
# from .views import me, hello_dashboard 

# urlpatterns = [
#     path('me/', me, name='user-me'),
#     path('hello/',hello_dashboard),
# ]
from django.urls import path
from . import views

urlpatterns = [
    path('api/me/', views.me, name='me'),
    path('api/dashboard/', views.hello_dashboard, name='hello_dashboard'),
    path('api/leave-balances/', views.get_leave_balances, name='leave_balances'),
    path('api/notifications/', views.get_notifications, name='notifications'),
    path('api/recent-activities/', views.get_recent_activities, name='recent_activities'),
    path('api/calendar-days/', views.get_calendar_days, name='calendar_days'),
]