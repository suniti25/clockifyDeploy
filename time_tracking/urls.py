from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.TimeProjectListCreateView.as_view(), name="time_project_list"),
    path(
        "projects/<int:pk>/",
        views.TimeProjectDetailView.as_view(),
        name="time_project_detail",
    ),
    path("entries/", views.TimeEntryListCreateView.as_view(), name="time_entry_list"),
    path("entries/recent/", views.TimeEntryRecentView.as_view(), name="time_entry_recent"),
    path(
        "entries/latest-by-user/",
        views.TimeEntryLatestByUserView.as_view(),
        name="time_entry_latest_by_user",
    ),
    path("entries/running/", views.TimeEntryRunningView.as_view(), name="time_entry_running"),
    path("entries/start/", views.TimeEntryStartView.as_view(), name="time_entry_start"),
    path("entries/stop/", views.TimeEntryStopView.as_view(), name="time_entry_stop"),
    path(
        "entries/<int:pk>/continue/",
        views.TimeEntryContinueView.as_view(),
        name="time_entry_continue",
    ),
    path(
        "entries/<int:pk>/duplicate/",
        views.TimeEntryDuplicateView.as_view(),
        name="time_entry_duplicate",
    ),
    path("entries/<int:pk>/", views.TimeEntryDetailView.as_view(), name="time_entry_detail"),
]
