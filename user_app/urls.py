from django.urls import path
from .views import me, hello_dashboard 

urlpatterns = [
    path('me/', me, name='user-me'),
    path('hello/',hello_dashboard),
]