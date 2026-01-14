from django.urls import path
from . import views

urlpatterns = [
    path('api/apply_leave/', views.apply_leave, name='apply_leave'), 
    path('api/requests/', views.get_requests, name='get_requests'),
]