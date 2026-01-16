from django.urls import path
from . import views

urlpatterns = [
    path('apply_leave/', views.apply_leave, name='apply_leave'), 
    path('requests/', views.get_requests, name='get_requests'),
    path('leave/<int:pk>/', views.update_leave, name='update_leave'),  
]
