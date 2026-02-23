from django.urls import path
from integrations import views

urlpatterns = [
    path("admin/google/connect/", views.google_connect),
    path(
        "admin/google/callback/",
        views.google_callback,
        name="integrations_google_callback",
    ),
]
