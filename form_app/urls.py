from django.urls import path
from . import views

urlpatterns = [
    path("apply_leave/", views.apply_leave, name="apply_leave"),
    path("requests/", views.get_requests, name="get_requests"),
    path("leave/update/", views.update_leave_by_body, name="update_leave_by_body"),
]
