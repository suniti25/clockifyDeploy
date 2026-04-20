from django.urls import path

from . import views

urlpatterns = [
    path("projects/", views.TimeProjectListCreateView.as_view()),
    path("projects/<int:pk>/", views.TimeProjectDetailView.as_view()),
    path("entries/", views.TimeEntryListCreateView.as_view()),
    path("entries/running/", views.TimeEntryRunningView.as_view()),
    path("entries/start/", views.TimeEntryStartView.as_view()),
    path("entries/stop/", views.TimeEntryStopView.as_view()),
    path("entries/<int:pk>/continue/", views.TimeEntryContinueView.as_view()),
    path("entries/<int:pk>/duplicate/", views.TimeEntryDuplicateView.as_view()),
    path("entries/<int:pk>/", views.TimeEntryDetailView.as_view()),
]
