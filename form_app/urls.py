from django.urls import path
from .views import apply_leave

urlpatterns = [
    path('leaves/', apply_leave),
]
